"""Fresh-content, single- and multi-chunk tests with KV migration evidence."""

import json
import math
from pathlib import Path
import re
import urllib.request
import uuid

from disagg_config import urls


def fetch(url, timeout, body=None):
    request = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode()


def metrics(text):
    result = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([\w:]+)(\{.*\})?\s+([^\s]+)(?:\s+\S+)?", line)
        if not match:
            raise ValueError("unrecognized Prometheus sample: " + line)
        name, raw, number = match.groups()
        labels = tuple(
            sorted(
                (m[1], json.loads('"' + m[2] + '"'))
                for m in re.finditer(r'(\w+)="((?:\\.|[^"\\])*)"', raw or "")
            )
        )
        value = float(number)
        if not math.isfinite(value):
            continue
        result[name, labels] = value
    return result


def deltas(before, after):
    result = {}
    for key in before.keys() | after.keys():
        if not key[0].endswith("_total"):
            continue
        if key not in after or after[key] < before.get(key, 0):
            raise ValueError("metric disappeared/reset: " + repr(key))
        result[key] = after[key] - before.get(key, 0)
    return result


def total(samples, name, **labels):
    matches = [
        v
        for (n, ls), v in samples.items()
        if n == name and all(dict(ls).get(k) == val for k, val in labels.items())
    ]
    if not matches:
        raise ValueError("required metric missing: " + name + " " + str(labels))
    return sum(matches)


def migration_evidence(before, after, chunks):
    delta = {
        role: deltas(metrics(before[role]), metrics(after[role])) for role in before
    }
    p, d = delta["prefill"], delta["decode"]
    result = dict(
        migrations=total(
            p, "kv_manager_engine_commands_total", kind="migrate", result="ok"
        ),
        wire_bytes=total(p, "kv_manager_bytes_moved_total", leg="wire"),
        source_bytes=total(p, "kv_manager_dp_device_bytes_total", direction="d2h"),
        destination_bytes=total(d, "kv_manager_dp_device_bytes_total", direction="h2d"),
    )
    if result["migrations"] < 36 * chunks:
        raise ValueError(f"insufficient layer migrations: {result}")
    if any(result[k] <= 0 for k in ("wire_bytes", "source_bytes", "destination_bytes")):
        raise ValueError("no end-to-end migrated bytes: " + str(result))
    for role, samples in delta.items():
        # Require the failure family even when all samples are zero.
        total(samples, "kv_manager_dp_rdma_batch_failures_total")
        for (name, labels), value in samples.items():
            labels = dict(labels)
            failure = (
                "failures" in name
                or "errors" in name
                or (
                    name == "kv_manager_engine_commands_total"
                    and labels.get("result") != "ok"
                )
            )
            if failure and value > 0:
                raise ValueError(f"{role}: new failure counter {name}: {value}")
    return result


def smoke(site, directory, get=fetch):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    endpoint, timeout = urls(site), site["timeouts"]["request"]
    summary = {"passed": False, "tests": []}

    def save(name, value):
        (directory / name).write_text(
            value if isinstance(value, str) else json.dumps(value, indent=2) + "\n"
        )

    def health():
        for role in ("prefill", "decode"):
            value = json.loads(get(endpoint[role + "_kvm"] + "/health", timeout))
            if value.get("status") != "healthy":
                raise ValueError(role + " KVM unhealthy: " + str(value))

    try:
        health()
        models = json.loads(get(endpoint["api"] + "/models", timeout))
        save("models.json", models)
        if not any(m["id"] == site["model_name"] for m in models.get("data", [])):
            raise ValueError("configured model is not advertised")
        for name, repeats, chunks in (("fresh", 65, 1), ("multichunk", 520, 2)):
            code = "CODE-" + uuid.uuid4().hex[:12].upper()
            body = dict(
                model=site["model_name"],
                messages=[
                    dict(
                        role="user",
                        content=(
                            f"Remember this exact access code: {code}.\n"
                            + "This paragraph is padding and does not change the code.\n"
                            * repeats
                            + "\nWhat access code did I give at the beginning? Reply only with that code."
                        ),
                    )
                ],
                max_tokens=64,
                temperature=0.0,
                stream=False,
                chat_template_kwargs={"enable_thinking": False},
            )
            save(name + "-request.json", body)
            before = {}
            for role in ("prefill", "decode"):
                before[role] = get(endpoint[role + "_kvm"] + "/metrics", timeout)
                save(f"{name}-{role}-before.txt", before[role])
            raw = get(endpoint["api"] + "/chat/completions", timeout, body)
            save(name + "-response.json", raw)
            response = json.loads(raw)
            after = {}
            for role in ("prefill", "decode"):
                after[role] = get(endpoint[role + "_kvm"] + "/metrics", timeout)
                save(f"{name}-{role}-after.txt", after[role])
            choice = response["choices"][0]
            if (
                choice["message"]["content"].strip() != code
                or choice["finish_reason"] != "stop"
            ):
                raise ValueError(name + ": wrong/incomplete answer")
            tokens = response["usage"]["prompt_tokens"]
            if (
                not 256 < tokens <= 8192 - body["max_tokens"]
                or math.ceil(tokens / 5120) != chunks
            ):
                raise ValueError(
                    f"{name}: unexpected token/chunk count {tokens}; adjust fixture for tokenizer"
                )
            evidence = migration_evidence(before, after, chunks)
            health()
            summary["tests"].append(
                dict(
                    name=name,
                    request_id=response.get("id"),
                    prompt_tokens=tokens,
                    answer=code,
                    **evidence,
                )
            )
            print(
                f'{name}: correct answer, {tokens} prompt tokens, {evidence["migrations"]:g} migrations'
            )
        summary["passed"] = True
        return summary
    except Exception as exc:
        summary["error"] = str(exc)
        raise
    finally:
        save("summary.json", summary)
