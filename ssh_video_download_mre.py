"""Minimal SSH video download reproducer.

This uses the same SSH style as video_annotation_v12.py:
no tty, no ControlMaster, no persistent ControlPath.
"""
import argparse
import os
import posixpath
import shlex
import subprocess
import sys
import tempfile


DEFAULT_HOST = "Guangdong4090"
DEFAULT_REMOTE_VIDEO = (
    "/mnt/gaokejun/comet-1.5k/videos/task-0001/"
    "observation.images.rgb.head/episode_00010000.mp4"
)


def ssh_args(host, command, verbose=False):
    args = [
        "ssh",
        "-T",
        "-o", "BatchMode=yes",
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
        "-o", "ClearAllForwardings=yes",
    ]
    if verbose:
        args.append("-vvv")
    args.extend([host, command])
    return args


def run_probe(host, remote_path, verbose=False):
    q = shlex.quote(remote_path)
    command = (
        "set -e; "
        f"echo HOST=$(hostname); "
        f"echo USER=$(whoami); "
        f"test -r {q}; "
        f"stat -c 'SIZE=%s PATH=%n' -- {q}; "
        f"command -v head >/dev/null && echo HEAD=ok"
    )
    proc = subprocess.run(
        ssh_args(host, command, verbose=verbose),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    print("== probe ==")
    print(f"returncode: {proc.returncode}")
    print("-- stdout --")
    print(proc.stdout.decode("utf-8", errors="replace"))
    print("-- stderr --")
    print(proc.stderr.decode("utf-8", errors="replace"))
    return proc.returncode


def download(host, remote_path, local_path, bytes_limit=None, verbose=False):
    q = shlex.quote(remote_path)
    if bytes_limit is None:
        command = f"cat -- {q}"
        label = "full cat"
    else:
        command = f"head -c {int(bytes_limit)} -- {q}"
        label = f"head -c {int(bytes_limit)}"

    os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
    print(f"== download: {label} ==")
    print(f"host: {host}")
    print(f"remote: {remote_path}")
    print(f"local: {local_path}")
    with open(local_path, "wb") as f:
        proc = subprocess.run(
            ssh_args(host, command, verbose=verbose),
            stdout=f,
            stderr=subprocess.PIPE,
        )

    size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
    print(f"returncode: {proc.returncode}")
    print(f"local bytes: {size}")
    print("-- stderr --")
    print(proc.stderr.decode("utf-8", errors="replace"))
    if proc.returncode != 0:
        try:
            os.remove(local_path)
        except OSError:
            pass
    return proc.returncode


def default_local_path(remote_path, bytes_limit):
    suffix = ".part" if bytes_limit is not None else ""
    return os.path.join(
        tempfile.gettempdir(),
        "ssh_video_download_mre",
        posixpath.basename(remote_path) + suffix,
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Minimal SSH video download test.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--remote-video", default=DEFAULT_REMOTE_VIDEO)
    parser.add_argument("--local-out")
    parser.add_argument("--bytes", type=int, default=1024 * 1024,
                        help="Bytes to download with head -c. Use 0 with --full for full video.")
    parser.add_argument("--full", action="store_true",
                        help="Download the whole file with cat, matching the annotator.")
    parser.add_argument("--skip-probe", action="store_true")
    parser.add_argument("--verbose-ssh", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    bytes_limit = None if args.full else max(1, int(args.bytes))
    local_out = args.local_out or default_local_path(args.remote_video, bytes_limit)

    if not args.skip_probe:
        rc = run_probe(args.host, args.remote_video, verbose=args.verbose_ssh)
        if rc != 0:
            return rc

    return download(
        args.host,
        args.remote_video,
        local_out,
        bytes_limit=bytes_limit,
        verbose=args.verbose_ssh,
    )


if __name__ == "__main__":
    sys.exit(main())
