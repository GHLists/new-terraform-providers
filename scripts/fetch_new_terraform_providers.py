#!/usr/bin/env python3
"""Fetch providers newly published to the Terraform Registry.

The Terraform Registry's website API caps its provider listing at a few
hundred entries and the registry protocol has no timestamps at all, so this
script takes the provider list from the [OpenTofu registry mirror](
https://github.com/opentofu/registry): a single git-tree listing of
``providers/`` yields every ``namespace/name`` (about 4,600 providers).

That list is diffed against the previous run's baseline. For every unseen
provider, the registry detail endpoint
``https://registry.terraform.io/v1/providers/<namespace>/<name>`` provides
the ``published_at`` of the provider's latest version — for a newly
registered provider this equals its creation. Only providers published
inside the requested window are listed.

The baseline of known providers is stored as a compressed text file in the
repo so the next run resumes from the previous state.
"""

import argparse
import csv
import datetime as dt
import http.client
import json
import lzma
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TREES_URL = "https://api.github.com/repos/opentofu/registry/git/trees/main?recursive=1"
PROVIDER_URL = "https://registry.terraform.io/v1/providers/{namespace}/{name}"
DEFAULT_USER_AGENT = (
    "new-terraform-providers/1.0 (https://github.com/GHLists/new-terraform-providers)"
)

BASELINE_LIMIT = 50_000
DESCRIPTION_LIMIT = 300
CSV_HEADER = (
    "created_at",
    "provider",
    "namespace",
    "version",
    "description",
)
BASELINE_SUFFIX = ".txt.lzma"

TRANSIENT_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    json.JSONDecodeError,
    http.client.HTTPException,
    OSError,
)


class NotFound(Exception):
    pass


def iso(moment):
    moment = moment.astimezone(dt.timezone.utc)
    if moment.microsecond:
        fraction = f"{moment.microsecond:06d}".rstrip("0")
        return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction}Z"
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value):
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def timestamp_filename(moment):
    moment = moment.astimezone(dt.timezone.utc)
    stamp = moment.strftime("%Y-%m-%dT%H-%M-%S")
    if moment.microsecond:
        stamp += "-" + f"{moment.microsecond:06d}".rstrip("0")
    return stamp + "Z"


def fetch_json(url, user_agent, retries=3, backoff=5.0):
    last_error = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise NotFound(url) from error
            last_error = error
        except TRANSIENT_ERRORS as error:
            last_error = error
        if attempt < retries:
            print(f"attempt {attempt} failed ({last_error}), retrying", file=sys.stderr)
            time.sleep(backoff * attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def clean_text(value, limit=DESCRIPTION_LIMIT):
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return text


def fetch_provider_keys(user_agent, retries):
    """Return the set of ``namespace/name`` provider keys known to OpenTofu."""
    tree = fetch_json(TREES_URL, user_agent, retries=retries)
    entries = tree.get("tree")
    if not isinstance(entries, list):
        raise RuntimeError("GitHub tree response does not contain entries")
    if tree.get("truncated"):
        raise RuntimeError("GitHub tree response was truncated")
    providers = set()
    for entry in entries:
        path = entry.get("path") or ""
        if not path.startswith("providers/") or not path.endswith(".json"):
            continue
        parts = path[len("providers/") : -len(".json")].split("/")
        if len(parts) != 3 or not parts[1] or not parts[2]:
            continue
        providers.add(f"{parts[1]}/{parts[2]}")
    if not providers:
        raise RuntimeError("OpenTofu registry tree contained no providers")
    return providers


def fetch_provider_detail(key, user_agent, retries):
    """Fetch the registry detail endpoint, returning None when unavailable."""
    namespace, _, name = key.partition("/")
    url = PROVIDER_URL.format(
        namespace=urllib.parse.quote(namespace, safe=""),
        name=urllib.parse.quote(name, safe=""),
    )
    try:
        return fetch_json(url, user_agent, retries=retries)
    except NotFound:
        return None


def build_row(key, detail):
    namespace, _, name = key.partition("/")
    return {
        "created_at": iso(parse_timestamp(detail["published_at"])),
        "provider": key,
        "namespace": namespace,
        "version": clean_text(detail.get("version"), 20),
        "description": clean_text(detail.get("description")),
    }


def write_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def baseline_path(output_dir, manifest):
    stored = manifest.get("baseline", {}).get("path")
    if stored:
        return Path(stored)
    return Path(output_dir) / f"terraform-providers-baseline{BASELINE_SUFFIX}"


def read_baseline_text(path):
    """Read the baseline from disk, or fall back to the committed copy.

    The workflow checks out only ``scripts`` from the repository, so the
    baseline can be missing from the working tree even though it is committed.
    """
    try:
        return lzma.decompress(path.read_bytes()).decode("utf-8")
    except (OSError, lzma.LZMAError):
        pass
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{path.as_posix()}"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    try:
        return lzma.decompress(result.stdout).decode("utf-8")
    except lzma.LZMAError:
        return None


def load_baseline(path):
    text = read_baseline_text(path)
    if text is None:
        return None
    return {line for line in text.splitlines() if line}


def save_baseline(path, keys):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = lzma.compress(("\n".join(sorted(keys)) + "\n").encode("utf-8"))
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(blob)
    os.replace(temporary, path)


def read_manifest_text(path):
    """Read the manifest from disk, or fall back to the committed copy."""
    manifest_path = Path(path)
    try:
        return manifest_path.read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{manifest_path.as_posix()}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def load_manifest(path):
    text = read_manifest_text(path)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"manifest {path} is not valid JSON") from error
    if not isinstance(data, dict):
        raise RuntimeError(f"manifest {path} must contain a JSON object")
    version = data.get("state_version", 1)
    if version != 1:
        raise RuntimeError(f"manifest {path} has an unsupported state version")
    return data


def save_manifest(path, manifest):
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_name(f".{manifest_path.name}.tmp")
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, manifest_path)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        help="UTC start timestamp as ISO 8601 (default: end of the last list)",
    )
    parser.add_argument(
        "--until",
        help="UTC end timestamp as ISO 8601 (default: now)",
    )
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--manifest", default="latest.json")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=1.0,
        help="window length when no previous list exists (default: 1)",
    )
    parser.add_argument(
        "--api-delay",
        type=float,
        default=0.3,
        help="seconds between registry requests (default: 0.3)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    until = parse_timestamp(args.until) if args.until else now
    manifest = load_manifest(args.manifest)

    if args.since:
        since = parse_timestamp(args.since)
        if "window" in manifest:
            stored_window = parse_timestamp(manifest["window"])
            if since < stored_window:
                raise RuntimeError(
                    "backfill would move the window backwards; "
                    f"the manifest window is {iso(stored_window)}"
                )
    elif "window" in manifest:
        since = parse_timestamp(manifest["window"])
    else:
        since = until - dt.timedelta(hours=args.lookback_hours)

    providers = fetch_provider_keys(args.user_agent, args.retries)

    output = baseline_path(args.output_dir, manifest)
    known = load_baseline(output)
    if known is None:
        save_baseline(output, providers)
        manifest["baseline"] = {
            "path": output.as_posix(),
            "count": len(providers),
        }
        manifest["window"] = iso(until)
        manifest["source_truncated"] = False
        save_manifest(args.manifest, manifest)
        print(
            f"seeded baseline with {len(providers)} providers; "
            "next runs produce the first list"
        )
        return 0

    fresh = sorted(set(providers) - known)
    rows = []
    skipped = 0
    for key in fresh:
        detail = fetch_provider_detail(key, args.user_agent, args.retries)
        if not isinstance(detail, dict) or not detail.get("published_at"):
            skipped += 1
            continue
        try:
            row = build_row(key, detail)
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        created = parse_timestamp(row["created_at"])
        if created <= since or created > until:
            continue
        rows.append(row)
        time.sleep(args.api_delay)
    if skipped:
        print(f"skipped {skipped} providers without registry details", file=sys.stderr)
    rows.sort(key=lambda row: row["created_at"])

    save_baseline(output, providers)
    manifest["baseline"] = {
        "path": output.as_posix(),
        "count": len(providers),
    }
    manifest["window"] = iso(until)
    manifest["source_truncated"] = False
    if rows:
        output_csv = (
            Path(args.output_dir)
            / f"new-terraform-providers-{timestamp_filename(until)}.csv"
        )
        write_csv(output_csv, rows)
        manifest["list"] = {
            "path": output_csv.as_posix(),
            "from": iso(since),
            "to": iso(until),
            "count": len(rows),
        }
        print(
            f"wrote {len(rows)} providers created between {iso(since)} "
            f"and {iso(until)} to {output_csv}"
        )
    else:
        print(f"no new providers between {iso(since)} and {iso(until)}")
    save_manifest(args.manifest, manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
