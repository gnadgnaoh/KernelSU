#!/usr/bin/env python3
"""Monitor backslashxx/KernelSU (primary) with fallback to
ximi-mojito/android_kernel_xiaomi_mojito (personal branch, drivers/kernelsu/).

Updates:
  - kernel/                  when new kernel-side KernelSU commits appear
  - .github/sync-state.json  sync metadata
  - docs/README.md           only the top SYNC-MOJITO block (rest untouched)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATE_PATH = os.path.join(ROOT, ".github", "sync-state.json")
README_PATH = os.path.join(ROOT, "docs", "README.md")
KERNEL_DIR = os.path.join(ROOT, "kernel")

UPSTREAM_REPO = "backslashxx/KernelSU"
MOJITO_REPO = "ximi-mojito/android_kernel_xiaomi_mojito"
MOJITO_BRANCH = "personal"
MOJITO_SUBDIR = "drivers/kernelsu"

START_MARKER = "<!-- SYNC-MOJITO:START -->"
END_MARKER = "<!-- SYNC-MOJITO:END -->"


def api_get(path):
    p = path.lstrip("/")
    if shutil.which("gh"):
        res = subprocess.run(
            ["gh", "api", p],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if res.returncode == 0:
            return 200, json.loads(res.stdout)
        m = re.search(r"HTTP (\d{3})", res.stderr)
        return (int(m.group(1)) if m else 500), None
    url = f"https://api.github.com/{p}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "xxKSU-sync-mojito",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, None


def check_upstream_ksu():
    """Return (status_str, default_branch, head_sha, commit_date)."""
    code, data = api_get(f"repos/{UPSTREAM_REPO}")
    if code != 200 or not isinstance(data, dict):
        return "unavailable", "", "", ""
    if data.get("archived"):
        return "archived", data.get("default_branch", ""), "", ""
    branch = data.get("default_branch", "master")
    c_code, c_data = api_get(f"repos/{UPSTREAM_REPO}/commits/{branch}")
    if c_code != 200 or not isinstance(c_data, dict):
        return "online", branch, "", ""
    sha = c_data.get("sha", "")
    date = ((c_data.get("commit") or {}).get("committer") or {}).get("date", "")[:10]
    return "online", branch, sha, date


def latest_mojito_commit():
    code, data = api_get(
        f"repos/{MOJITO_REPO}/commits?sha={MOJITO_BRANCH}&path={MOJITO_SUBDIR}&per_page=1"
    )
    if code != 200 or not isinstance(data, list) or not data:
        raise RuntimeError(f"Failed to query {MOJITO_REPO}@{MOJITO_BRANCH} ({code})")
    top = data[0]
    sha = top["sha"]
    date = ((top.get("commit") or {}).get("committer") or {}).get("date", "")[:10]
    subject = ((top.get("commit") or {}).get("message") or "").splitlines()[0]
    return sha, date, subject


def sync_subdir(repo, branch, subdir):
    """Sparse-clone repo@branch and mirror subdir into kernel/."""
    with tempfile.TemporaryDirectory(prefix="xxksu-sync-") as tmp:
        url = f"https://github.com/{repo}.git"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse",
             "--branch", branch, url, tmp],
            check=True,
        )
        subprocess.run(
            ["git", "-C", tmp, "sparse-checkout", "set", "--no-cone", f"/{subdir}"],
            check=True,
        )
        src = os.path.join(tmp, subdir)
        if not os.path.isdir(src):
            raise RuntimeError(f"Missing {subdir} in {repo}@{branch}")
        if os.path.isdir(KERNEL_DIR):
            shutil.rmtree(KERNEL_DIR)
        shutil.copytree(src, KERNEL_DIR)


def parse_ksu_version():
    makefile = os.path.join(KERNEL_DIR, "Makefile")
    if not os.path.isfile(makefile):
        return ""
    text = open(makefile, encoding="utf-8", errors="replace").read()
    m = re.search(r"KSU_VERSION=(\d+)", text)
    return m.group(1) if m else ""


def build_readme_block(state):
    active_repo = state["active_source"]
    active_branch = state["active_branch"]
    if active_repo == UPSTREAM_REPO:
        sha = state.get("upstream_ksu_sha", "")
        date = state.get("mojito_date", "")
        src_line = (
            f"Synced from [`{UPSTREAM_REPO}` (`{active_branch}` branch)]"
            f"(https://github.com/{UPSTREAM_REPO}/tree/{active_branch})"
        )
        commit_url = f"https://github.com/{UPSTREAM_REPO}/commit/{sha}" if sha else ""
        status_note = f"`{UPSTREAM_REPO}` is back online."
    else:
        sha = state.get("mojito_sha", "")
        date = state.get("mojito_date", "")
        src_line = (
            f"Synced from backslashxx's kernel-side KernelSU updates in "
            f"[`{MOJITO_REPO}` (`{MOJITO_BRANCH}` branch)]"
            f"(https://github.com/{MOJITO_REPO}/tree/{MOJITO_BRANCH})"
        )
        commit_url = f"https://github.com/{MOJITO_REPO}/commit/{sha}" if sha else ""
        status_note = (
            f"`{UPSTREAM_REPO}` is currently {state.get('upstream_ksu_status', 'unavailable')}; "
            f"falling back to `{MOJITO_REPO}`."
        )

    short = sha[:8] if sha else "unknown"
    commit_md = f"[`{short}`]({commit_url})" if commit_url else f"`{short}`"
    kver = state.get("ksu_version") or parse_ksu_version()
    ver_part = f" (`KSU_VERSION={kver}`)" if kver else ""
    date_part = f" on {date}" if date else ""
    final_sha = state.get("final_sha", "9a02a5c1")

    return "\n".join([
        START_MARKER,
        "> [!NOTE]",
        f"> **Branch `sync-mojito`:** {src_line} (latest synced commit: {commit_md}{date_part}{ver_part}). "
        f"It is named `sync-mojito` because the standalone `{UPSTREAM_REPO}` repository was removed and "
        f"subsequent kernel-side updates are pulled from that mojito kernel branch.",
        ">",
        f"> **Monitor status:** {status_note} Automated builds and releases are published in "
        f"[Releases](https://github.com/Najip/xxKSU/releases).",
        ">",
        f"> Backslashxx's last untouched commit from `{UPSTREAM_REPO}` (`{final_sha}`) is preserved on the "
        f"[`final`](https://github.com/Najip/xxKSU/tree/final) branch.",
        END_MARKER,
    ])


def update_readme(state):
    text = open(README_PATH, encoding="utf-8").read()
    block = build_readme_block(state)
    if START_MARKER in text and END_MARKER in text:
        new_text = re.sub(
            re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
            block,
            text,
            count=1,
            flags=re.S,
        )
    else:
        # Replace an existing top [!NOTE] block if present, otherwise prepend.
        m = re.match(r"^> \[!NOTE\]\n(?:>.*\n)+\n?", text)
        if m:
            new_text = block + "\n\n" + text[m.end():]
        else:
            new_text = block + "\n\n" + text
    if new_text != text:
        with open(README_PATH, "w", encoding="utf-8") as f:
            f.write(new_text)
        return True
    return False


def write_output(pairs):
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not gh_out:
        return
    with open(gh_out, "a", encoding="utf-8") as f:
        for k, v in pairs.items():
            f.write(f"{k}={v}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-sync", action="store_true")
    parser.add_argument("--force-release", action="store_true")
    args = parser.parse_args()

    state = json.load(open(STATE_PATH, encoding="utf-8"))
    prev_upstream_status = state.get("upstream_ksu_status", "unavailable")

    up_status, up_branch, up_sha, up_date = check_upstream_ksu()
    state["upstream_ksu_status"] = up_status
    upstream_restored = up_status == "online" and prev_upstream_status != "online"

    changed = False
    commit_msg = ""
    release_tag = ""
    release_notes = ""

    if up_status == "online" and up_sha:
        state["active_source"] = UPSTREAM_REPO
        state["active_branch"] = up_branch
        if args.force_sync or up_sha != state.get("upstream_ksu_sha"):
            sync_subdir(UPSTREAM_REPO, up_branch, "kernel")
            state["upstream_ksu_sha"] = up_sha
            state["mojito_date"] = up_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            state["ksu_version"] = parse_ksu_version() or state.get("ksu_version", "")
            changed = True
            commit_msg = f"kernel: sync upstream {UPSTREAM_REPO}@{up_sha[:8]}"
            release_tag = f"v3.3.1-upstream-{state['mojito_date'].replace('-', '')}-{up_sha[:8]}"
            release_notes = f"Synced from `{UPSTREAM_REPO}` (`{up_branch}` @ `{up_sha[:8]}`)."
    else:
        state["active_source"] = MOJITO_REPO
        state["active_branch"] = MOJITO_BRANCH
        sha, date, subject = latest_mojito_commit()
        if args.force_sync or sha != state.get("mojito_sha"):
            sync_subdir(MOJITO_REPO, MOJITO_BRANCH, MOJITO_SUBDIR)
            state["mojito_sha"] = sha
            state["mojito_date"] = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            state["ksu_version"] = parse_ksu_version() or state.get("ksu_version", "")
            changed = True
            commit_msg = f"kernel: sync {MOJITO_REPO}@{sha[:8]} ({subject})"
            release_tag = f"v3.3.1-mojito-{state['mojito_date'].replace('-', '')}-{sha[:8]}"
            release_notes = (
                f"Synced `kernel/` from `{MOJITO_REPO}` (`{MOJITO_BRANCH}` @ `{sha[:8]}`): {subject}."
            )

    readme_changed = update_readme(state)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")

    if not release_tag:
        short = (state.get("upstream_ksu_sha") or state.get("mojito_sha") or "HEAD")[:8]
        day = (state.get("mojito_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")).replace("-", "")
        prefix = "upstream" if state["active_source"] == UPSTREAM_REPO else "mojito"
        release_tag = f"v3.3.1-{prefix}-{day}-{short}"
        release_notes = f"Build from `{state['active_source']}` (`{state['active_branch']}` @ `{short}`)."

    should_commit = changed or readme_changed or (up_status != prev_upstream_status)
    should_release = changed or args.force_release

    write_output({
        "changed": "true" if should_commit else "false",
        "kernel_changed": "true" if changed else "false",
        "should_release": "true" if should_release else "false",
        "upstream_restored": "true" if upstream_restored else "false",
        "upstream_status": up_status,
        "upstream_branch": up_branch,
        "upstream_sha": up_sha[:8] if up_sha else "",
        "commit_msg": commit_msg or "docs: update sync-mojito status",
        "release_tag": release_tag,
        "release_notes": release_notes,
    })
    print(json.dumps({
        "should_commit": should_commit,
        "kernel_changed": changed,
        "should_release": should_release,
        "upstream_restored": upstream_restored,
        "release_tag": release_tag,
    }))


if __name__ == "__main__":
    main()
