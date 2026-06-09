"""视频帧标注工具 v12。

变化 vs v11：
1. **sparse marks**：marked_frames 改为 list[Optional[int]]，None = 该边界未标
2. **插入/删除/移动 subtask 不再清空 marks**：尽量保留未被破坏的边界位置
   - 插入位置 k 中间：在新 boundary k-1 和 k 处插 None（旧 boundary k-1 被破坏，丢弃）
   - 插入头/尾：在对应位置插 None，其它全保留
   - 删除位置 k 中间：丢 boundary k-1 和 k，新 boundary k-1 (=None)，其它保留
   - 删除头：丢 boundary 0；删除尾：丢 boundary N-2
   - 上下移：把跨越的 boundary 标 None（语义变了，需要重标），其它保留
3. **seek bar 加粗**（tk.Scale + width=24），改用扁平浅色
4. JSON 兼容：保存时**仅含已完成的非 None marks**（外部脚本旧逻辑不变）
   若部分未标，JSON 是不完整列表（如 N 个 subtask 期望 N-1 个边界，实际 < N-1 个）
"""
import glob
import argparse
import copy
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

import cv2
from PIL import Image, ImageTk

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
from vis_segment import render_segment_video


VIDEO_MAX_W = 720
VIDEO_MAX_H = 480
SUBTASK_PANEL_W = 560
EPISODE_RE = re.compile(r"^episode_(\d+)_annotation_check\.mp4$", re.IGNORECASE)
EPISODE_ID_RE = re.compile(r"episode_(\d+)", re.IGNORECASE)
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}

# 配色
BG = "#f5f7fa"
CARD = "#ffffff"
BORDER = "#e1e4e8"
TEXT = "#1f2937"
MUTED = "#6b7280"
ACCENT = "#3b82f6"
ACCENT_DARK = "#2563eb"
SUCCESS = "#10b981"
WARNING = "#f59e0b"
DANGER = "#ef4444"
EDIT_BG = "#fef3c7"
PENDING_BG = "#f3f4f6"

FONT_FAMILY = "Segoe UI"
FONT_BODY = (FONT_FAMILY, 10)
FONT_BODY_BOLD = (FONT_FAMILY, 10, "bold")
FONT_SMALL = (FONT_FAMILY, 9)
FONT_HEADER = (FONT_FAMILY, 11, "bold")


LOCAL_HOSTS = {"", "local", "localhost", "127.0.0.1", "::1"}


def is_remote_host(host):
    return bool(host) and host.strip().lower() not in LOCAL_HOSTS


def path_module(remote=False):
    return posixpath if remote else os.path


def path_basename(path):
    return re.split(r"[\\/]+", path.rstrip("/\\"))[-1]


def path_dirname(path, remote=False):
    return path_module(remote).dirname(path)


def path_join(*parts, remote=False):
    return path_module(remote).join(*parts)


class LocalIO:
    is_remote = False

    def exists(self, path):
        return os.path.exists(path)

    def is_dir(self, path):
        return os.path.isdir(path)

    def read_json(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def write_json(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def makedirs(self, path):
        os.makedirs(path, exist_ok=True)

    def remove(self, path):
        try:
            os.remove(path)
        except OSError:
            pass

    def cache_video(self, path):
        return path

    def upload_file(self, local_path, remote_path):
        if os.path.abspath(local_path) != os.path.abspath(remote_path):
            os.makedirs(os.path.dirname(remote_path), exist_ok=True)
            shutil.copyfile(local_path, remote_path)
        return True

    def list_videos(self, video_dir):
        return find_videos_in_folder(video_dir)

    def find_episode_jsons(self, input_dir, stem_json, variant_hint=None):
        out = []
        for root, dirs, files in os.walk(input_dir):
            dirs[:] = [d for d in dirs if d.lower() not in {"viz", "human_anno"}]
            if stem_json not in files:
                continue
            p = os.path.join(root, stem_json)
            if variant_hint and variant_hint not in os.path.normpath(p).lower().split(os.sep):
                continue
            out.append(p)
        return out


class RemoteIO:
    is_remote = True

    def __init__(self, host, cache_dir=None):
        self.host = host
        host_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", host)
        self.cache_dir = cache_dir or os.path.join(tempfile.gettempdir(), "video_annotation_remote_cache", host_key)
        self.state_dir = os.path.join(self.cache_dir, "state")
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.state_dir, exist_ok=True)

    def _ssh_args(self, command):
        return [
            "ssh",
            "-T",
            "-o", "BatchMode=yes",
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
            "-o", "ClearAllForwardings=yes",
            self.host,
            command,
        ]

    def _scp_args(self, local_path, remote_path, legacy=False):
        args = [
            "scp",
            "-q",
            "-o", "BatchMode=yes",
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
            "-o", "ClearAllForwardings=yes",
        ]
        if legacy:
            args.append("-O")
        args.extend([
            local_path,
            f"{self.host}:{remote_path}",
        ])
        return args

    def _ssh(self, command, *, input_bytes=None, capture=True, check=True):
        kwargs = {
            "input": input_bytes,
            "check": check,
        }
        if capture:
            kwargs.update({"stdout": subprocess.PIPE, "stderr": subprocess.PIPE})
        return subprocess.run(self._ssh_args(command), **kwargs)

    def local_state_path(self, remote_path):
        digest = hashlib.sha1(f"{self.host}:{remote_path}".encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.state_dir, f"{digest}_{path_basename(remote_path)}")

    def mkdir_p(self, path):
        if not path:
            return
        q = shlex.quote(path)
        errors = []
        for attempt in range(1, 4):
            proc = self._ssh(f"mkdir -p -- {q}", capture=True, check=False)
            if proc.returncode == 0:
                return
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            errors.append(f"attempt {attempt}: rc={proc.returncode}, stderr={stderr or '(empty)'}")
            time.sleep(0.5 * attempt)
        raise RuntimeError(
            f"Remote mkdir failed after {len(errors)} attempts.\n"
            f"host: {self.host}\n"
            f"remote dir: {path}\n"
            f"details:\n" + "\n".join(errors)
        )

    def exists(self, path):
        q = shlex.quote(path)
        return self._ssh(f"test -f {q}", capture=False, check=False).returncode == 0

    def is_dir(self, path):
        q = shlex.quote(path)
        return self._ssh(f"test -d {q}", capture=False, check=False).returncode == 0

    def read_json(self, path):
        q = shlex.quote(path)
        proc = self._ssh(f"cat -- {q}")
        return json.loads(proc.stdout.decode("utf-8"))

    def write_json(self, path, data):
        directory = posixpath.dirname(path)
        if directory:
            self.makedirs(directory)
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        q = shlex.quote(path)
        self._ssh(f"cat > {q}", input_bytes=payload, capture=True)

    def makedirs(self, path):
        self.mkdir_p(path)

    def remove(self, path):
        q = shlex.quote(path)
        self._ssh(f"rm -f {q}", capture=False, check=False)

    def cache_video(self, path):
        stem = path_basename(path)
        digest = hashlib.sha1(f"{self.host}:{path}".encode("utf-8")).hexdigest()[:16]
        local_path = os.path.join(self.cache_dir, f"{digest}_{stem}")
        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            return local_path
        q = shlex.quote(path)
        tmp_path = local_path + ".part"
        errors = []
        for attempt in range(1, 4):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            with open(tmp_path, "wb") as f:
                proc = subprocess.run(self._ssh_args(f"cat -- {q}"), stdout=f, stderr=subprocess.PIPE)
            if proc.returncode == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                os.replace(tmp_path, local_path)
                return local_path
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            errors.append(f"attempt {attempt}: rc={proc.returncode}, stderr={stderr or '(empty)'}")
            time.sleep(0.8 * attempt)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise RuntimeError(
            f"SSH video download failed after {len(errors)} attempts.\n"
            f"host: {self.host}\n"
            f"remote path: {path}\n"
            f"details:\n" + "\n".join(errors)
        )

    def upload_file(self, local_path, remote_path):
        if self.exists(remote_path):
            print(f"[skip existing remote] {remote_path}")
            return False
        directory = posixpath.dirname(remote_path)
        if directory:
            self.mkdir_p(directory)
        errors = []
        for legacy in (False, True):
            proc = subprocess.run(
                self._scp_args(local_path, remote_path, legacy=legacy),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if proc.returncode == 0:
                return True
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            mode = "legacy scp (-O)" if legacy else "default scp/sftp"
            errors.append(f"{mode}: rc={proc.returncode}, stderr={stderr or '(empty)'}")
        if errors:
            raise RuntimeError(
                f"SCP upload failed.\n"
                f"host: {self.host}\n"
                f"local path: {local_path}\n"
                f"remote path: {remote_path}\n"
                f"details:\n" + "\n".join(errors)
            )
        return True

    def list_videos(self, video_dir):
        q = shlex.quote(video_dir)
        cmd = (
            f"find {q} -path '*/human_anno/*' -prune -o -type f "
            r"\( -iname '*.mp4' -o -iname '*.avi' -o -iname '*.mov' -o -iname '*.mkv' -o -iname '*.webm' \) -print"
        )
        proc = self._ssh(cmd)
        out = []
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            episode_id = parse_episode_id(line)
            if episode_id is not None:
                out.append((episode_id, line))
        out.sort(key=lambda item: (item[0], posixpath.relpath(item[1], video_dir).lower()))
        return out

    def find_episode_jsons(self, input_dir, stem_json, variant_hint=None):
        q_dir = shlex.quote(input_dir)
        q_name = shlex.quote(stem_json)
        cmd = (
            f"find {q_dir} -path '*/viz/*' -prune -o -path '*/human_anno/*' -prune "
            f"-o -type f -name {q_name} -print"
        )
        proc = self._ssh(cmd)
        out = []
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            if variant_hint and variant_hint not in posixpath.normpath(line).lower().split("/"):
                continue
            out.append(line)
        return out


def make_io(host):
    return RemoteIO(host) if is_remote_host(host) else LocalIO()


def find_raw_annotations_in_cwd():
    matches = sorted(glob.glob("*_raw_annotations.json"))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        messagebox.showerror("Error", f"No *_raw_annotations.json in:\n{os.getcwd()}")
        return None
    return filedialog.askopenfilename(
        title="Select raw annotations JSON",
        initialdir=os.getcwd(),
        filetypes=[("JSON", "*_raw_annotations.json")],
    ) or None


def find_videos_in_cwd():
    out = []
    for p in glob.glob("episode_*_annotation_check.mp4"):
        m = EPISODE_RE.match(os.path.basename(p))
        if m:
            out.append((int(m.group(1)), p))
    out.sort()
    return out


def boundaries_path(video_path):
    return os.path.splitext(video_path)[0] + ".json"


def edits_path(video_path):
    return os.path.splitext(video_path)[0].replace("_annotation_check", "") + "_subtasks_edited.json"


def parse_episode_id(path):
    m = EPISODE_ID_RE.search(os.path.basename(path))
    return int(m.group(1)) if m else None


def clean_episode_stem(video_path):
    stem = os.path.splitext(path_basename(video_path))[0]
    for suffix in ("_annotation_check", "_annotated"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
    return stem


def find_videos_in_folder(video_dir):
    out = []
    for root, dirs, files in os.walk(video_dir):
        dirs[:] = [d for d in dirs if d != "human_anno"]
        for name in files:
            if os.path.splitext(name)[1].lower() not in VIDEO_EXTENSIONS:
                continue
            path = os.path.join(root, name)
            episode_id = parse_episode_id(path)
            if episode_id is None:
                continue
            out.append((episode_id, path))
    out.sort(key=lambda item: (item[0], os.path.relpath(item[1], video_dir).lower()))
    return out


def load_task_metadata(mapping_path, task_name, io=None):
    io = io or LocalIO()
    data = io.read_json(mapping_path)
    tasks = data.get("tasks", data)
    if task_name not in tasks:
        available = sorted(k for k, v in tasks.items() if isinstance(v, dict))
        raise KeyError(f"Task '{task_name}' not found. Available examples: {available[:20]}")
    meta = tasks[task_name]
    if not isinstance(meta, dict):
        raise ValueError(f"Task '{task_name}' metadata is not an object")
    if not isinstance(meta.get("task_progress_order"), list):
        raise ValueError(f"Task '{task_name}' has no task_progress_order list")
    return meta


def load_task_metadata_by_id(mapping_path, task_id, io=None):
    io = io or LocalIO()
    data = io.read_json(mapping_path)
    tasks = data.get("tasks", data)
    task_id = int(task_id)
    for name, meta in tasks.items():
        if not isinstance(meta, dict):
            continue
        try:
            meta_task_index = int(meta.get("task_index", -1))
        except (TypeError, ValueError):
            continue
        if meta_task_index == task_id:
            if not isinstance(meta.get("task_progress_order"), list):
                raise ValueError(f"Task id {task_id} / '{name}' has no task_progress_order list")
            return name, meta
    available = sorted(
        int(v.get("task_index")) for v in tasks.values()
        if isinstance(v, dict) and isinstance(v.get("task_index"), int)
    )
    raise KeyError(f"Task id {task_id} not found. Available examples: {available[:20]}")


def parse_episode_range(range_text):
    if not range_text:
        return None
    text = str(range_text).strip()
    if not text:
        return None
    if ":" in text:
        left, right = text.split(":", 1)
    elif "-" in text:
        left, right = text.split("-", 1)
    else:
        left = right = text
    start = int(left) if left.strip() else None
    end = int(right) if right.strip() else None
    if start is not None and end is not None and start > end:
        raise ValueError("--episode-range start must be <= end")
    return start, end


def episode_in_range(episode_id, task_id, episode_range):
    if episode_range is None:
        return True
    start, end = episode_range
    local_idx = episode_id - int(task_id) * 10000
    value = local_idx
    if (start is not None and start >= 10000) or (end is not None and end >= 10000):
        value = episode_id
    if start is not None and value < start:
        return False
    if end is not None and value > end:
        return False
    return True


def filter_videos_by_episode_range(videos, task_id, range_text):
    episode_range = parse_episode_range(range_text)
    return [(n, p) for n, p in videos if episode_in_range(n, task_id, episode_range)]


def task_folder_name(task_id):
    return f"task-{int(task_id):04d}"


def default_task_video_dir(root_dir, task_id, remote=False):
    folder = task_folder_name(task_id)
    pm = path_module(remote)
    root_base = path_basename(pm.normpath(root_dir)).lower()
    if root_base == "videos":
        return path_join(root_dir, folder, "observation.images.rgb.head", remote=remote)
    return path_join(root_dir, "videos", folder, "observation.images.rgb.head", remote=remote)


def build_episode_paths_from_range(video_dir, task_id, range_text, remote=False):
    episode_range = parse_episode_range(range_text)
    if episode_range is None:
        raise ValueError("--episode-range is required when startup video listing is disabled")
    start, end = episode_range
    if start is None or end is None:
        raise ValueError("--episode-range must be closed, for example 0:10 or 10000:10010")
    use_full_ids = start >= 10000 or end >= 10000
    episodes = range(start, end + 1) if use_full_ids else range(start + int(task_id) * 10000, end + int(task_id) * 10000 + 1)
    return [
        (episode_id, path_join(video_dir, f"episode_{episode_id:08d}.mp4", remote=remote))
        for episode_id in episodes
    ]


def resolve_task_video_dir(root_dir, task_id, io=None):
    io = io or LocalIO()
    remote = io.is_remote
    folder = task_folder_name(task_id)
    candidates = [
        path_join(root_dir, "videos", folder, "observation.images.rgb.head", remote=remote),
        path_join(root_dir, folder, "observation.images.rgb.head", remote=remote),
    ]
    for candidate in candidates:
        if io.is_dir(candidate):
            return candidate
    raise FileNotFoundError(
        "No task video folder found. Tried:\n" + "\n".join(candidates)
    )


def resolve_task_annotation_dir(root_dir, task_id, io=None):
    io = io or LocalIO()
    remote = io.is_remote
    folder = task_folder_name(task_id)
    candidates = [
        path_join(root_dir, "annotations", folder, remote=remote),
        path_join(root_dir, folder, remote=remote),
    ]
    for candidate in candidates:
        if io.is_dir(candidate):
            return candidate
    return None


def mapping_subtasks(task_meta):
    subtasks = []
    for idx, item in enumerate(task_meta.get("task_progress_order", [])):
        if not isinstance(item, dict):
            item = {"subtask": str(item)}
        rec = {
            "order": idx,
            "key": item.get("key", f"subtask_{idx}"),
            "subtask": item.get("subtask", ""),
        }
        if item.get("notes"):
            rec["notes"] = item.get("notes")
        subtasks.append(rec)
    return subtasks


def default_mapping_path():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, "behavior_subtask_mapping.json")


def output_json_path_for_video(video_path, input_dir, output_dir, remote=False):
    pm = path_module(remote)
    rel = pm.relpath(video_path, input_dir)
    sep = "/" if remote else os.sep
    parts = [p for p in rel.split(sep) if p.lower() != "viz"]
    if not parts:
        parts = [path_basename(video_path)]
    parts[-1] = clean_episode_stem(video_path) + ".json"
    return path_join(output_dir, *parts, remote=remote)


def state_json_path_for_output(output_json_path):
    base = os.path.splitext(output_json_path)[0]
    return base + "_marks.json"


def edits_path_for_output(output_json_path):
    base = os.path.splitext(output_json_path)[0]
    return base + "_subtasks_edited.json"


def vis_path_for_output(output_json_path):
    base = os.path.splitext(output_json_path)[0]
    return base + "_vis.mp4"


def template_candidates_for_video(video_path, input_dir, io=None):
    io = io or LocalIO()
    remote = io.is_remote
    pm = path_module(remote)
    stem_json = clean_episode_stem(video_path) + ".json"
    candidates = []

    rel = pm.relpath(video_path, input_dir)
    sep = "/" if remote else os.sep
    parts = rel.split(sep)
    if "viz" in [p.lower() for p in parts]:
        no_viz = [p for p in parts if p.lower() != "viz"]
        no_viz[-1] = stem_json
        candidates.append(path_join(input_dir, *no_viz, remote=remote))

    cur = path_dirname(video_path, remote=remote)
    for _ in range(4):
        candidates.append(path_join(cur, stem_json, remote=remote))
        cur = path_dirname(cur, remote=remote)

    norm = pm.normpath(video_path)
    video_parts = norm.split("/" if remote else os.sep)
    variant_hint = None
    for part in video_parts:
        if part.lower().startswith("variant_") or part.lower() == "baseline":
            variant_hint = part.lower()
            break
    candidates.extend(io.find_episode_jsons(input_dir, stem_json, variant_hint))

    seen = set()
    out = []
    for p in candidates:
        ap = pm.normpath(p)
        if ap not in seen:
            seen.add(ap)
            out.append(p)
    return out


def load_episode_template(video_path, input_dir, io=None):
    io = io or LocalIO()
    for candidate in template_candidates_for_video(video_path, input_dir, io=io):
        if io.exists(candidate):
            return io.read_json(candidate), candidate
    return None, None


def load_episode_template_from_dirs(video_path, template_dirs, io=None):
    io = io or LocalIO()
    stem_json = clean_episode_stem(video_path) + ".json"
    for directory in template_dirs or []:
        if not directory:
            continue
        candidate = path_join(directory, stem_json, remote=io.is_remote)
        if io.exists(candidate):
            return io.read_json(candidate), candidate
    for directory in template_dirs or []:
        if not directory:
            continue
        template, path = load_episode_template(video_path, directory, io=io)
        if template is not None:
            return template, path
    return None, None


def build_fallback_episode(task_name, task_meta, video_path, total_frames, fps):
    subtasks = mapping_subtasks(task_meta)
    sample_interval = 30
    sampled_indices = list(range(0, max(total_frames, 1), sample_interval))
    if sampled_indices and sampled_indices[-1] != max(total_frames - 1, 0):
        sampled_indices.append(max(total_frames - 1, 0))
    return {
        "task": task_name,
        "task_index": task_meta.get("task_index"),
        "task_prompt": task_meta.get("task_prompt", ""),
        "video_path": video_path,
        "model": "human",
        "total_frames": total_frames,
        "fps": fps,
        "sample_interval": sample_interval,
        "num_sampled_frames": len(sampled_indices),
        "sampled_indices": sampled_indices,
        "subtasks": subtasks,
        "segments": [],
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def sampled_index_range_for_frame_range(sampled_indices, start, end):
    if not sampled_indices:
        return [0, 0]
    first = None
    last = None
    for i, frame in enumerate(sampled_indices):
        if first is None and frame >= start:
            first = i
        if frame <= end:
            last = i
    if first is None:
        first = len(sampled_indices) - 1
    if last is None:
        last = 0
    if first > last:
        last = first
    return [first, last]


def build_human_episode_json(template, task_name, task_meta, video_path, marked_frames, total_frames, fps):
    data = copy.deepcopy(template) if template else build_fallback_episode(
        task_name, task_meta, video_path, total_frames, fps
    )
    total_frames = int(total_frames or data.get("total_frames") or 1)
    fps = float(fps or data.get("fps") or 30.0)
    data["task"] = task_name
    data["task_index"] = task_meta.get("task_index", data.get("task_index"))
    data["task_prompt"] = task_meta.get("task_prompt", data.get("task_prompt", ""))
    data["video_path"] = video_path
    data["model"] = "human"
    data["total_frames"] = total_frames
    data["fps"] = fps

    mapping_items = mapping_subtasks(task_meta)
    data["subtasks"] = mapping_items
    if len(data.get("segments", [])) != len(mapping_items):
        data["segments"] = [copy.deepcopy(item) for item in mapping_items]

    boundaries = [int(x) for x in marked_frames]
    starts = [0] + [b + 1 for b in boundaries]
    ends = boundaries + [max(0, total_frames - 1)]
    sampled_indices = data.get("sampled_indices", [])

    segments = []
    for idx, item in enumerate(mapping_items):
        seg = copy.deepcopy(data["segments"][idx]) if idx < len(data["segments"]) else {}
        seg["order"] = idx
        seg["key"] = item.get("key", f"subtask_{idx}")
        seg["subtask"] = item.get("subtask", "")
        if item.get("notes"):
            seg["notes"] = item.get("notes")
        start = max(0, min(int(starts[idx]), max(0, total_frames - 1)))
        end = max(0, min(int(ends[idx]), max(0, total_frames - 1)))
        seg["frame_range"] = [start, end]
        seg["time_range_sec"] = [round(start / fps, 3), round(end / fps, 3)]
        if sampled_indices:
            seg["sampled_index_range"] = sampled_index_range_for_frame_range(sampled_indices, start, end)
        seg["rationale"] = "Human annotated segment."
        segments.append(seg)
    data["segments"] = segments
    data["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return data


def _blend(hex_a, hex_b, t):
    """t=0→a, t=1→b。"""
    def h2rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    a, b = h2rgb(hex_a), h2rgb(hex_b)
    out = tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3))
    return "#%02x%02x%02x" % out


def setup_styles(root):
    style = ttk.Style(root)
    try: style.theme_use("clam")
    except tk.TclError: pass

    style.configure("Card.TFrame", background=CARD)
    style.configure("Bg.TFrame", background=BG)

    style.configure("TLabel", background=BG, foreground=TEXT, font=FONT_BODY)
    style.configure("Card.TLabel", background=CARD, foreground=TEXT, font=FONT_BODY)
    style.configure("Header.TLabel", background=BG, foreground=TEXT, font=FONT_HEADER)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=FONT_SMALL)
    style.configure("Pill.TLabel", background=ACCENT, foreground="white",
                    font=FONT_BODY_BOLD, padding=(8, 4))

    def make_btn(name, bg, fg="white", hover_bg=None):
        hover_bg = hover_bg or bg
        style.configure(name, background=bg, foreground=fg, font=FONT_BODY,
                        relief="flat", borderwidth=0, padding=(10, 6))
        style.map(name,
                  background=[("active", hover_bg), ("disabled", "#cbd5e1")],
                  foreground=[("disabled", "#94a3b8")])

    make_btn("TButton", "#e5e7eb", TEXT, "#d1d5db")
    make_btn("Accent.TButton", ACCENT, "white", ACCENT_DARK)
    make_btn("Success.TButton", SUCCESS, "white", "#059669")
    make_btn("Warning.TButton", WARNING, "white", "#d97706")
    make_btn("Danger.TButton", DANGER, "white", "#dc2626")
    make_btn("Ghost.TButton", CARD, ACCENT, "#eff6ff")
    make_btn("Mini.TButton", "#e5e7eb", TEXT, "#d1d5db")
    style.configure("Mini.TButton", padding=(4, 2), font=FONT_SMALL)
    make_btn("MiniDanger.TButton", "#fee2e2", DANGER, "#fecaca")
    style.configure("MiniDanger.TButton", padding=(4, 2), font=FONT_SMALL)

    style.configure("TEntry", fieldbackground=CARD, foreground=TEXT, padding=4)

    root.configure(bg=BG)


class VideoMarkerApp:
    def __init__(self, root, summary_json_path, video_list, task_name=None,
                 task_meta=None, input_dir=None, output_dir=None, io=None, host=None,
                 template_dirs=None):
        self.root = root
        self.folder_mode = summary_json_path is None
        self.io = io or LocalIO()
        self.host = host or "local"
        self.remote_mode = self.io.is_remote
        self.task_name = task_name or os.path.basename(summary_json_path).replace("_raw_annotations.json", "")
        self.root.title(f"Video Annotator v12 — {self.task_name}")

        setup_styles(root)

        self.fps = 20
        self.frame_interval_ms = int(1000 / self.fps)
        self.summary_json_path = summary_json_path
        self.video_list = video_list
        self.video_idx = 0
        self.task_meta = task_meta or {}
        if self.remote_mode:
            self.input_dir = posixpath.normpath(input_dir) if input_dir else None
            self.output_dir = posixpath.normpath(output_dir) if output_dir else None
        else:
            self.input_dir = os.path.abspath(input_dir) if input_dir else None
            self.output_dir = os.path.abspath(output_dir) if output_dir else None
        self.template_dirs = template_dirs or [self.input_dir]
        self.output_json_path = None
        self.output_state_path = None
        self.output_edits_path = None
        self.output_vis_path = None
        self.local_output_json_path = None
        self.local_state_path = None
        self.local_edits_path = None
        self.local_output_vis_path = None
        self.template_json = None
        self.template_json_path = None
        self.local_video_path = None
        self.current_total_frames = 0
        self.current_video_fps = 0.0

        if summary_json_path:
            with open(summary_json_path) as f:
                self.summary_data = json.load(f)
        else:
            self.summary_data = []

        self.cap = None
        self.video_path = None
        self.current_frame = -1
        self.marked_frames = []  # list[Optional[int]]，长度 == expected_marks
        self.is_playing = False
        self.saved_once = False

        self.original_subtasks = []
        self.current_subtasks = []
        self.edit_log = []
        self.expected_marks = 0

        self._subtask_widgets = []

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._auto_load_first)
        self.root.after(self.frame_interval_ms, self.update_frame)

    # ---------- 边界状态 helpers ----------

    def _ensure_marks_len(self):
        """marks 长度对齐 expected_marks。"""
        while len(self.marked_frames) < self.expected_marks:
            self.marked_frames.append(None)
        if len(self.marked_frames) > self.expected_marks:
            self.marked_frames = self.marked_frames[:self.expected_marks]

    def _next_pending_boundary(self):
        for i, m in enumerate(self.marked_frames):
            if m is None: return i
        return None

    def _marks_complete(self):
        return self.expected_marks > 0 and all(m is not None for m in self.marked_frames)

    def _marked_count(self):
        return sum(1 for m in self.marked_frames if m is not None)

    def _max_boundary_frame(self):
        total = int(self.current_total_frames or 0)
        if total <= 1:
            return 0
        return total - 2

    def _boundary_frame_error(self):
        if self.expected_marks == 0 or self.current_total_frames <= 1:
            return ""
        max_boundary = self._max_boundary_frame()
        bad = [
            (idx, int(frame))
            for idx, frame in enumerate(self.marked_frames)
            if frame is not None and int(frame) > max_boundary
        ]
        if not bad:
            return ""
        details = ", ".join(f"boundary {idx + 1}=frame {frame}" for idx, frame in bad)
        return (
            f"{details} 超过最大允许 boundary 帧 {max_boundary}。\n"
            "最后一个 subtask 会自动结束在视频最后一帧；boundary 必须标在最后一帧之前，"
            "否则下一个 subtask 没有有效帧。"
        )

    def _effective_task_meta(self):
        meta = copy.deepcopy(self.task_meta)
        items = list(meta.get("task_progress_order", []))
        new_items = []
        for idx, text in enumerate(self.current_subtasks):
            if idx < len(items) and isinstance(items[idx], dict):
                item = copy.deepcopy(items[idx])
            else:
                item = {"key": f"subtask_{idx}", "notes": "Edited in annotation UI."}
            item["subtask"] = text
            new_items.append(item)
        meta["task_progress_order"] = new_items
        return meta

    def _local_sidecar_path(self, path):
        if self.remote_mode and path:
            return self.io.local_state_path(path)
        return path

    def _read_local_json(self, path, default=None):
        if not path or not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_local_json(self, path, data):
        if not path:
            return
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _remove_local_file(self, path):
        if not path:
            return
        try:
            os.remove(path)
        except OSError:
            pass

    # ---------- UI ----------

    def _build_ui(self):
        # header
        header = ttk.Frame(self.root, style="Bg.TFrame")
        header.pack(side=tk.TOP, fill=tk.X, padx=14, pady=(12, 6))
        title_row = ttk.Frame(header, style="Bg.TFrame")
        title_row.pack(fill=tk.X)
        self.title_label = ttk.Label(title_row, text=self.task_name, style="Header.TLabel")
        self.title_label.pack(side=tk.LEFT)
        self.progress_pill = ttk.Label(title_row, text="", style="Pill.TLabel")
        self.progress_pill.pack(side=tk.RIGHT)

        # 顶部导航条：集间跳转
        nav = ttk.Frame(header, style="Bg.TFrame")
        nav.pack(fill=tk.X, pady=(8, 0))
        self.prev_btn = ttk.Button(nav, text="←  Prev", style="TButton", width=10,
                                    command=self.prev_episode, state=tk.DISABLED)
        self.prev_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.jump_unmarked_btn = ttk.Button(nav, text="→  Next Unmarked",
                                             style="Accent.TButton", width=16,
                                             command=self.jump_to_next_unmarked, state=tk.DISABLED)
        self.jump_unmarked_btn.pack(side=tk.LEFT, padx=4)
        ttk.Label(nav, text="Jump ep #", style="Muted.TLabel").pack(side=tk.LEFT, padx=(12, 4))
        self.jump_ep_var = tk.StringVar()
        self.jump_ep_entry = ttk.Entry(nav, textvariable=self.jump_ep_var, width=5, font=FONT_BODY)
        self.jump_ep_entry.pack(side=tk.LEFT)
        self.jump_ep_entry.bind("<Return>", lambda e: self.jump_to_ep())
        ttk.Button(nav, text="Go", style="TButton", width=4,
                   command=self.jump_to_ep).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(nav, text="↺  Revert subtasks", style="Ghost.TButton",
                   command=self._edit_revert_all).pack(side=tk.RIGHT)

        # 主内容
        content = ttk.Frame(self.root, style="Bg.TFrame")
        content.pack(expand=True, fill=tk.BOTH, padx=14, pady=8)

        video_card = tk.Frame(content, bg=CARD, highlightbackground=BORDER,
                              highlightthickness=1, bd=0)
        video_card.pack(side=tk.LEFT, padx=(0, 10))
        video_wrap = tk.Frame(video_card, width=VIDEO_MAX_W, height=VIDEO_MAX_H, bg="#000000")
        video_wrap.pack(padx=8, pady=8)
        video_wrap.pack_propagate(False)
        self.video_label = tk.Label(video_wrap, bg="#000000")
        self.video_label.pack(expand=True, fill=tk.BOTH)

        right_card = tk.Frame(content, bg=CARD, highlightbackground=BORDER,
                               highlightthickness=1, bd=0, width=SUBTASK_PANEL_W)
        right_card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right_card.pack_propagate(False)

        right_head = tk.Frame(right_card, bg=CARD)
        right_head.pack(fill=tk.X, padx=14, pady=(12, 6))
        tk.Label(right_head, text="Subtasks", font=FONT_HEADER, fg=TEXT, bg=CARD).pack(side=tk.LEFT)
        tk.Label(right_head, text="点文本框可直接编辑", font=FONT_SMALL, fg=MUTED, bg=CARD).pack(side=tk.LEFT, padx=(8, 0))

        self.instruction_label = tk.Label(right_card, text="", anchor="w", justify=tk.LEFT,
                                           font=FONT_BODY, fg="#1e40af", bg="#eff6ff",
                                           padx=10, pady=8, wraplength=SUBTASK_PANEL_W - 60, bd=0)
        self.instruction_label.pack(fill=tk.X, padx=14, pady=(0, 8))
        # 跟随 right_card 实际宽度自动换行
        def _resize_instr(event):
            self.instruction_label.config(wraplength=max(200, event.width - 60))
        right_card.bind("<Configure>", _resize_instr)

        list_wrap = tk.Frame(right_card, bg=CARD)
        list_wrap.pack(expand=True, fill=tk.BOTH, padx=10, pady=(0, 8))
        self._canvas = tk.Canvas(list_wrap, highlightthickness=0, bg=CARD)
        scrollbar = ttk.Scrollbar(list_wrap, orient="vertical", command=self._canvas.yview)
        self.subtask_container = tk.Frame(self._canvas, bg=CARD)
        self.subtask_container.bind("<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas_window = self._canvas.create_window((0, 0), window=self.subtask_container, anchor="nw")
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfig(self._canvas_window, width=e.width))
        self._canvas.configure(yscrollcommand=scrollbar.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._canvas.bind_all("<MouseWheel>",
            lambda e: self._canvas.yview_scroll(int(-e.delta / 120), "units"))

        self.expected_label = tk.Label(right_card, text="", font=FONT_BODY_BOLD,
                                        fg=DANGER, bg="#fef2f2", padx=10, pady=8, bd=0)
        self.expected_label.pack(fill=tk.X, padx=14, pady=(0, 12))

        # ===== 底部控制条（极简：6 个按钮 + info + Save） =====
        ctrl_card = tk.Frame(self.root, bg=CARD, highlightbackground=BORDER,
                              highlightthickness=1, bd=0)
        ctrl_card.pack(side=tk.BOTTOM, fill=tk.X, padx=14, pady=(0, 8))

        control_frame = tk.Frame(ctrl_card, bg=CARD)
        control_frame.pack(fill=tk.X, padx=12, pady=10)

        # 组 1：播放
        self.play_pause_btn = ttk.Button(control_frame, text="▶  Play",
                                          style="Accent.TButton", width=10,
                                          command=self.toggle_play_pause, state=tk.DISABLED)
        self.play_pause_btn.pack(side=tk.LEFT, padx=2)

        # 组 2：标记 + 撤销
        ttk.Separator(control_frame, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=10)
        self.mark_btn = ttk.Button(control_frame, text="●  标记 (M)",
                                    style="Warning.TButton", width=12,
                                    command=self.mark_current_frame, state=tk.DISABLED)
        self.mark_btn.pack(side=tk.LEFT, padx=2)
        self.undo_btn = ttk.Button(control_frame, text="↶  撤销",
                                    style="TButton", width=8,
                                    command=self.undo_mark, state=tk.DISABLED)
        self.undo_btn.pack(side=tk.LEFT, padx=2)

        # 组 3：单帧步进
        ttk.Separator(control_frame, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=10)
        self.rewind_btn = ttk.Button(control_frame, text="◀  −1帧",
                                      style="TButton", width=8,
                                      command=self.step_back_frame, state=tk.DISABLED)
        self.rewind_btn.pack(side=tk.LEFT, padx=2)
        self.forward_btn = ttk.Button(control_frame, text="+1帧  ▶",
                                       style="TButton", width=8,
                                       command=self.step_forward_frame, state=tk.DISABLED)
        self.forward_btn.pack(side=tk.LEFT, padx=2)

        # 信息（居中扩展）
        self.info_label = tk.Label(control_frame, text="", fg=MUTED, bg=CARD, font=FONT_SMALL)
        self.info_label.pack(side=tk.LEFT, padx=20)

        # 组 4（右）：保存并下一集
        self.next_btn = ttk.Button(control_frame, text="保存并下一集  →",
                                    style="Success.TButton", width=18,
                                    command=self.next_episode, state=tk.DISABLED)
        self.next_btn.pack(side=tk.RIGHT, padx=2)

        # seek bar — 加粗 + 浅色扁平
        seek_card = tk.Frame(self.root, bg=BG)
        seek_card.pack(side=tk.BOTTOM, fill=tk.X, padx=14, pady=(0, 4))
        tk.Label(seek_card, text="Seek", fg=MUTED, bg=BG, font=FONT_SMALL).pack(side=tk.LEFT, padx=(0, 6))
        self._scale_updating_from_video = False
        self._seek_user_active = False
        self._was_playing_before_seek = False
        self.seek_scale = tk.Scale(seek_card, from_=0, to=100, orient=tk.HORIZONTAL,
                                    showvalue=True, length=600,
                                    command=self._on_seek_scale_change, state=tk.DISABLED,
                                    width=24, sliderlength=36,
                                    bg=BG, troughcolor="#dbe2ea", activebackground=ACCENT,
                                    fg=TEXT, font=FONT_SMALL,
                                    highlightthickness=0, bd=0, relief=tk.FLAT)
        self.seek_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.seek_scale.bind("<Button-1>", self._on_seek_press)
        self.seek_scale.bind("<ButtonRelease-1>", self._on_seek_release)

        self.root.bind("<Control-z>", lambda e: self.undo_mark())
        self.root.bind("<Control-Z>", lambda e: self.undo_mark())
        self.root.bind("<m>", self._on_m_key)
        self.root.bind("<M>", self._on_m_key)
        self.video_label.bind("<Button-1>", lambda e: self.root.focus_set())
        self.root.bind("<Button-1>", self._on_root_click, add="+")

    # ---------- 自动加载 ----------

    def _episode_is_complete(self, n, path):
        """检查一集是否标完：所有 subtask 非空 + 所有 boundary 已标。"""
        if self.folder_mode:
            out_json = output_json_path_for_video(path, self.input_dir, self.output_dir, remote=self.remote_mode)
            state_path = state_json_path_for_output(out_json)
            expected = max(0, len(mapping_subtasks(self.task_meta)) - 1)
            if self.remote_mode:
                local_state = self._local_sidecar_path(state_path)
                local_out = self._local_sidecar_path(out_json)
                if os.path.exists(local_state):
                    try:
                        marks = self._read_local_json(local_state, [])
                        if not isinstance(marks, list) or len(marks) < expected:
                            return False
                        if any(m is None for m in marks[:expected]):
                            return False
                        return os.path.exists(local_out)
                    except:
                        return False
                if not os.path.exists(local_out):
                    return False
                try:
                    d = self._read_local_json(local_out, {})
                    segments = d.get("segments", [])
                    return isinstance(segments, list) and len(segments) == expected + 1
                except:
                    return False
            if self.io.exists(state_path):
                try:
                    marks = self.io.read_json(state_path)
                    if not isinstance(marks, list) or len(marks) < expected:
                        return False
                    if any(m is None for m in marks[:expected]):
                        return False
                    return self.io.exists(out_json)
                except:
                    return False
            if not self.io.exists(out_json):
                return False
            try:
                d = self.io.read_json(out_json)
                segments = d.get("segments", [])
                return isinstance(segments, list) and len(segments) == expected + 1
            except:
                return False

        # subtasks（优先 edits，否则 raw）
        ep_idx = n - 1
        raw_subs = []
        if 0 <= ep_idx < len(self.summary_data) and isinstance(self.summary_data[ep_idx], dict):
            raw_subs = [str(x) for x in self.summary_data[ep_idx].get("subtasks", [])]
        subs = raw_subs
        ep_p = edits_path(path)
        if os.path.exists(ep_p):
            try:
                d = json.load(open(ep_p))
                subs = list(d.get("edited_subtasks", raw_subs))
            except: pass
        if any(not s.strip() for s in subs):
            return False
        expected = max(0, len(subs) - 1)
        if expected == 0:
            return True  # 单 subtask 无边界要标
        bp = boundaries_path(path)
        if not os.path.exists(bp):
            return False
        try:
            marks = json.load(open(bp))
            if not isinstance(marks, list): return False
            if len(marks) < expected: return False
            if any(m is None for m in marks[:expected]): return False
            return True
        except:
            return False

    def _find_resume_index(self):
        """找第一个未完成的集；都完成则返回 0。"""
        if self.remote_mode:
            return 0
        for j, (n, path) in enumerate(self.video_list):
            if not self._episode_is_complete(n, path):
                return j
        return 0  # 都标完了，从头看

    def _auto_load_first(self):
        if not self.video_list:
            messagebox.showerror("Error", "No videos."); self.root.after(100, self.root.destroy); return
        n_total = len(self.video_list)
        if self.remote_mode:
            resume_idx = 0
            resume_n = self.video_list[resume_idx][0]
            resume_msg = (
                f"Remote mode: {n_total} episodes in selected range.\n"
                f"Starting from ep_{resume_n}. Completed-count scan is skipped to avoid many SSH calls."
            )
        else:
            n_labeled = sum(1 for n, p in self.video_list if self._episode_is_complete(n, p))
            resume_idx = self._find_resume_index()
            resume_n = self.video_list[resume_idx][0]

            if n_labeled == n_total:
                resume_msg = f"全 {n_total} 集都已标完。从 ep_{resume_n} 开始浏览。"
            elif n_labeled == 0:
                resume_msg = f"开始标注。第一集：ep_{resume_n}。"
            else:
                resume_msg = f"上次进度：{n_labeled} / {n_total} 集已完成。\n继续 ep_{resume_n}（第一个未完成的集）。"

        intro = (
            f"Video Annotator v12 — {self.task_name}\n\n"
            f"{resume_msg}\n\n"
            "操作\n"
            "  • 点 subtask 文本框直接编辑\n"
            "  • 行尾  +↑ +↓ ↑ ↓ ✕  增删移\n"
            "  • 按 M / 点 ● 标记当前帧\n"
            "  • Ctrl+Z 撤销  ·  ◀ / ▶ 单帧步进\n"
            "  • 保存并下一集需所有 subtask 非空 + 所有 boundary 已标"
        )
        messagebox.showinfo("开始", intro)
        self._load_video_at_idx(resume_idx)

    def _load_video_at_idx(self, idx):
        if idx < 0 or idx >= len(self.video_list):
            messagebox.showinfo("Done", ""); return
        if self.video_path is not None:
            self._commit_all_subtask_edits()

        self.video_idx = idx
        n, path = self.video_list[idx]
        self._release_capture()

        try:
            capture_path = self.io.cache_video(path) if self.folder_mode else path
        except Exception as e:
            messagebox.showerror("Error", f"Cannot cache/read video:\n{path}\n\n{e}")
            if self.remote_mode:
                return
            self._advance_to_next()
            return

        cap = cv2.VideoCapture(capture_path)
        if not cap.isOpened():
            messagebox.showerror("Error", f"Cannot open {capture_path}")
            if not self.remote_mode:
                self._advance_to_next()
            return

        self.cap = cap; self.video_path = path
        self.local_video_path = capture_path
        self.current_frame = -1; self.is_playing = False; self.saved_once = False
        self.current_total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        self.current_video_fps = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0
        if self.folder_mode:
            self.output_json_path = output_json_path_for_video(
                path, self.input_dir, self.output_dir, remote=self.remote_mode
            )
            self.output_state_path = state_json_path_for_output(self.output_json_path)
            self.output_edits_path = edits_path_for_output(self.output_json_path)
            self.output_vis_path = vis_path_for_output(self.output_json_path)
            self.local_output_json_path = self._local_sidecar_path(self.output_json_path)
            self.local_state_path = self._local_sidecar_path(self.output_state_path)
            self.local_edits_path = self._local_sidecar_path(self.output_edits_path)
            self.local_output_vis_path = self._local_sidecar_path(self.output_vis_path)
            if self.remote_mode:
                self.template_json = None
                self.template_json_path = None
            else:
                self.template_json, self.template_json_path = load_episode_template_from_dirs(
                    path, self.template_dirs, io=self.io
                )
        else:
            self.output_json_path = None
            self.output_state_path = None
            self.output_edits_path = None
            self.output_vis_path = None
            self.local_output_json_path = None
            self.local_state_path = None
            self.local_edits_path = None
            self.local_output_vis_path = None
            self.template_json = None
            self.template_json_path = None

        self.original_subtasks = []
        task_instruction = ""
        if self.folder_mode:
            self.original_subtasks = [
                str(item.get("subtask", "")) for item in mapping_subtasks(self.task_meta)
            ]
            task_instruction = str(self.task_meta.get("task_prompt", ""))
        elif 1 <= n <= len(self.summary_data) and isinstance(self.summary_data[n - 1], dict):
            rec = self.summary_data[n - 1]
            self.original_subtasks = [str(x) for x in rec.get("subtasks", [])]
            task_instruction = str(rec.get("_task_instruction", "")) or str(rec.get("task", ""))
        self.instruction_label.config(
            text=f"📝  {task_instruction or self.task_name + '  (no instruction)'}"
        )

        ep = self.local_edits_path if self.folder_mode else edits_path(path)
        if os.path.exists(ep):
            try:
                with open(ep, encoding="utf-8") as f: d = json.load(f)
                self.current_subtasks = list(d.get("edited_subtasks", self.original_subtasks))
                self.edit_log = list(d.get("edit_log", []))
            except Exception as e:
                print(f"[warn] {ep}: {e}"); self.current_subtasks = list(self.original_subtasks); self.edit_log = []
        else:
            self.current_subtasks = list(self.original_subtasks); self.edit_log = []
        self.expected_marks = max(0, len(self.current_subtasks) - 1)

        bp = self.local_state_path if self.folder_mode else boundaries_path(path)
        if os.path.exists(bp):
            try:
                with open(bp, encoding="utf-8") as f: saved = json.load(f)
                if isinstance(saved, list):
                    self.marked_frames = [(int(x) if x is not None else None) for x in saved]
                else:
                    self.marked_frames = []
            except Exception as e:
                print(f"[warn] {bp}: {e}"); self.marked_frames = []
        elif self.folder_mode and self.local_output_json_path and os.path.exists(self.local_output_json_path):
            try:
                saved_episode = self._read_local_json(self.local_output_json_path, {})
                segments = saved_episode.get("segments", [])
                self.marked_frames = [
                    int(seg.get("frame_range", [None, None])[1])
                    for seg in segments[:-1]
                ]
            except Exception as e:
                print(f"[warn] {self.output_json_path}: {e}"); self.marked_frames = []
        else:
            self.marked_frames = []
        self._ensure_marks_len()

        self._render_subtasks()
        self._update_progress_pill()
        # 加载集时也滚到第一个待标（marks 可能预填了几个）
        self.root.after(100, self._scroll_to_next_pending)

        for btn in (self.play_pause_btn, self.mark_btn, self.undo_btn,
                    self.rewind_btn, self.forward_btn, self.next_btn,
                    self.prev_btn, self.jump_unmarked_btn):
            btn.config(state=tk.NORMAL)
        self.play_pause_btn.config(text="▶  Play")
        ret, frame = cap.read()
        if ret:
            self.current_frame = 0; self._show_frame(frame)
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) - 1
        self.seek_scale.config(state=tk.NORMAL, from_=0, to=max(0, total_frames))
        self._set_scale_from_video(0)
        self.title_label.config(text=f"{self.task_name}  ·  ep_{n}")
        self.root.title(f"v12 — {self.task_name} — ep_{n}")
        self._refresh_status_text(); self._refresh_marks_text()

    def _advance_to_next(self):
        if self.video_idx + 1 >= len(self.video_list):
            self._refresh_status_text()
            for btn in (self.play_pause_btn, self.mark_btn, self.undo_btn,
                        self.rewind_btn, self.forward_btn, self.prev_btn,
                        self.jump_unmarked_btn):
                btn.config(state=tk.NORMAL)
            self.next_btn.config(state=tk.DISABLED)
            messagebox.showinfo("Done", "Last episode reached.")
            return
        self._load_video_at_idx(self.video_idx + 1)

    def _update_progress_pill(self):
        n, _ = self.video_list[self.video_idx]
        if self.remote_mode:
            self.progress_pill.config(text=f"{self.video_idx + 1} / {len(self.video_list)}  ·  ep_{n}  ·  remote")
            return
        done = sum(1 for nn, p in self.video_list if self._episode_is_complete(nn, p))
        self.progress_pill.config(text=f"{self.video_idx + 1} / {len(self.video_list)}  ·  ep_{n}  ·  {done} done")

    # ---------- subtask 渲染 ----------

    def _calc_text_height(self, txt):
        if not txt: return 2
        lines = max(1, (len(txt) // 50) + (1 if len(txt) % 50 else 0))
        return min(5, max(2, lines))

    def _flash_subtask_row(self, idx, color):
        """re-render 之后让指定 subtask 行边框闪一下颜色（4 步淡出）。"""
        target = None
        for (i, w) in self._subtask_widgets:
            if i == idx:
                target = w; break
        if target is None: return
        outer = target.master.master  # Text → row → outer (Border Frame)
        orig = outer.cget("bg")
        # 颜色淡出 4 步
        steps = [color, color, _blend(color, orig, 0.5), orig]
        def _step(s=0):
            if s >= len(steps): return
            try: outer.configure(bg=steps[s])
            except: return
            self.root.after(100, lambda: _step(s + 1))
        _step()

    def _render_subtasks(self):
        for w in self.subtask_container.winfo_children():
            w.destroy()
        self._subtask_widgets = []
        self._boundary_widgets = {}  # bi → label widget
        edited_idxs = set()
        for i, s in enumerate(self.current_subtasks):
            if i >= len(self.original_subtasks) or s != self.original_subtasks[i]:
                edited_idxs.add(i)

        n = len(self.current_subtasks)
        for i, txt in enumerate(self.current_subtasks):
            self._render_subtask_row(i, txt, edited=(i in edited_idxs))
            if i < n - 1:
                self._render_boundary_row(i)
        tail = tk.Frame(self.subtask_container, bg=CARD)
        tail.pack(fill=tk.X, pady=(8, 4))
        ttk.Button(tail, text="+  Append subtask", style="Ghost.TButton",
                   command=lambda: self._edit_insert(len(self.current_subtasks))).pack()

        mc = self._marked_count()
        if mc == self.expected_marks and self.expected_marks > 0:
            self.expected_label.config(
                text=f"✓  All {self.expected_marks} boundaries marked",
                fg=SUCCESS, bg="#d1fae5")
        else:
            self.expected_label.config(
                text=f"{mc} / {self.expected_marks} boundaries marked",
                fg=DANGER, bg="#fef2f2")

    def _render_subtask_row(self, idx, txt, edited):
        bg = CARD
        outer = tk.Frame(self.subtask_container, bg=BORDER, padx=1, pady=1)
        outer.pack(fill=tk.X, pady=4, padx=4)
        row = tk.Frame(outer, bg=bg)
        row.pack(fill=tk.X, expand=True)

        head = tk.Frame(row, bg=bg)
        head.pack(fill=tk.X, padx=10, pady=(8, 4))
        tk.Label(head, text=f" {idx} ", bg=ACCENT, fg="white", font=FONT_SMALL,
                 padx=6, pady=1).pack(side=tk.LEFT)

        btns = tk.Frame(head, bg=bg)
        btns.pack(side=tk.RIGHT)
        ttk.Button(btns, text="+↑", style="Mini.TButton",
                   command=lambda i=idx: self._edit_insert(i)).pack(side=tk.LEFT, padx=1)
        ttk.Button(btns, text="+↓", style="Mini.TButton",
                   command=lambda i=idx: self._edit_insert(i + 1)).pack(side=tk.LEFT, padx=1)
        ttk.Button(btns, text="↑", style="Mini.TButton",
                   command=lambda i=idx: self._edit_move(i, -1)).pack(side=tk.LEFT, padx=1)
        ttk.Button(btns, text="↓", style="Mini.TButton",
                   command=lambda i=idx: self._edit_move(i, +1)).pack(side=tk.LEFT, padx=1)
        ttk.Button(btns, text="✕", style="MiniDanger.TButton",
                   command=lambda i=idx: self._edit_delete(i)).pack(side=tk.LEFT, padx=1)

        h = self._calc_text_height(txt)
        text_w = tk.Text(row, height=h, wrap="word", font=FONT_BODY,
                         bd=0, relief=tk.FLAT, bg="#fafbfc", fg=TEXT,
                         padx=10, pady=6,
                         highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        text_w.insert("1.0", txt)
        text_w.pack(fill=tk.X, padx=10, pady=(0, 10))
        text_w.bind("<FocusOut>", lambda e, i=idx: self._commit_subtask_edit(i))
        text_w.bind("<Control-Return>", lambda e, i=idx: (self._commit_subtask_edit(i), "break"))
        self._subtask_widgets.append((idx, text_w))

    def _scroll_to_next_pending(self):
        """滚到第一个未标的 boundary 行。"""
        bi = self._next_pending_boundary()
        if bi is None or bi not in self._boundary_widgets: return
        # 等布局完成
        self.subtask_container.update_idletasks()
        widget = self._boundary_widgets[bi]
        widget_y = widget.winfo_y()
        container_h = max(1, self.subtask_container.winfo_height())
        canvas_h = max(1, self._canvas.winfo_height())
        if container_h <= canvas_h: return  # 不需要滚
        # 让目标行落在 canvas 上 1/3 位置
        target_y = max(0, widget_y - canvas_h / 3)
        fraction = min(1.0, target_y / container_h)
        self._canvas.yview_moveto(fraction)

    def _render_boundary_row(self, bi):
        # sparse marks
        m = self.marked_frames[bi] if bi < len(self.marked_frames) else None
        if m is not None:
            bg, fg, dot = "#d1fae5", "#065f46", "●"
            text = f"  {dot}  boundary {bi + 1}  ·  frame {m}  (双击跳转 / 点重标)"
        else:
            # 第一个未标的高亮
            next_pending = self._next_pending_boundary()
            if bi == next_pending:
                bg, fg, dot = "#fef3c7", "#92400e", "◐"
                text = f"  {dot}  boundary {bi + 1}  ·  按 M 标记"
            else:
                bg, fg, dot = PENDING_BG, MUTED, "○"
                text = f"  {dot}  boundary {bi + 1}  ·  待标"
        lbl = tk.Label(self.subtask_container, text=text, bg=bg, fg=fg,
                       anchor="w", font=FONT_BODY_BOLD, padx=10, pady=6, bd=0)
        lbl.pack(fill=tk.X, padx=12, pady=2)
        self._boundary_widgets[bi] = lbl
        if m is not None:
            lbl.bind("<Double-Button-1>", lambda e, f=m: self._seek_to_frame(f))
            lbl.bind("<Button-1>", lambda e, i=bi: self._select_boundary_to_remark(i))

    def _select_boundary_to_remark(self, bi):
        """点已标 boundary → 把它清空，下次 M 会重标这个槽。"""
        if not messagebox.askyesno("重标这个 boundary?",
            f"清空 boundary {bi + 1}（当前 frame {self.marked_frames[bi]}），下次按 M 重标？"):
            return
        self.marked_frames[bi] = None
        self._save_marks_to_json(silent=True)
        self._render_subtasks()
        self._refresh_status_text(); self._refresh_marks_text()

    def _commit_subtask_edit(self, idx):
        if idx >= len(self.current_subtasks): return
        for (i, w) in self._subtask_widgets:
            if i == idx:
                new_text = w.get("1.0", "end-1c").strip()
                if new_text != self.current_subtasks[idx]:
                    old = self.current_subtasks[idx]
                    self.current_subtasks[idx] = new_text
                    self.edit_log.append({"op": "edit", "idx": idx, "old": old, "new": new_text})
                    self._save_edits_to_json()
                    self._render_subtasks()
                return

    def _commit_all_subtask_edits(self):
        for (idx, w) in list(self._subtask_widgets):
            if idx >= len(self.current_subtasks): continue
            new_text = w.get("1.0", "end-1c").strip()
            if new_text != self.current_subtasks[idx]:
                old = self.current_subtasks[idx]
                self.current_subtasks[idx] = new_text
                self.edit_log.append({"op": "edit", "idx": idx, "old": old, "new": new_text})
                self._save_edits_to_json()

    # ---------- 智能 insert / delete / move ----------

    def _edit_insert(self, idx):
        """在位置 idx 插入新 subtask。idx ∈ [0, N]。"""
        n_old = len(self.current_subtasks)
        self.current_subtasks.insert(idx, "")
        self.edit_log.append({"op": "insert", "idx": idx, "text": ""})
        n_new = len(self.current_subtasks)
        old_marks = list(self.marked_frames)
        # 新 boundary 数 = n_new - 1
        new_marks = [None] * (n_new - 1)
        if idx == 0:
            # 头插：新 boundary 0 是新的 (None); old marks 0..n_old-2 → new 1..n_old-1
            for i in range(n_old - 1):
                new_marks[i + 1] = old_marks[i]
        elif idx == n_old:
            # 尾插：新 boundary n_new-2 是新的 (None); old marks 0..n_old-2 不变
            for i in range(n_old - 1):
                new_marks[i] = old_marks[i]
        else:
            # 中插：old boundary idx-1 被破坏，丢；
            # old marks 0..idx-2 → new 0..idx-2 (保留)
            # new boundary idx-1, idx = None (新)
            # old marks idx..n_old-2 → new idx+1..n_new-2 (右移 1)
            for i in range(idx - 1):
                new_marks[i] = old_marks[i]
            # new_marks[idx - 1] = None  (already)
            # new_marks[idx] = None  (already)
            for i in range(idx, n_old - 1):
                new_marks[i + 1] = old_marks[i]
        self.marked_frames = new_marks
        self.expected_marks = n_new - 1
        self._save_edits_to_json()
        self._save_marks_to_json(silent=True)
        self._render_subtasks()
        self._refresh_marks_text(); self._refresh_status_text()
        for (i, w) in self._subtask_widgets:
            if i == idx: w.focus_set(); break

    def _edit_delete(self, idx):
        n_old = len(self.current_subtasks)
        if n_old <= 1:
            messagebox.showwarning("Can't delete", "至少要保留一个 subtask"); return
        old = self.current_subtasks.pop(idx)
        self.edit_log.append({"op": "delete", "idx": idx, "text": old})
        n_new = len(self.current_subtasks)
        old_marks = list(self.marked_frames)
        new_marks = [None] * (n_new - 1) if n_new > 1 else []
        if idx == 0:
            # 删头：丢 old boundary 0; old 1..n_old-2 → new 0..n_new-2
            for i in range(1, n_old - 1):
                new_marks[i - 1] = old_marks[i]
        elif idx == n_old - 1:
            # 删尾：丢 old boundary n_old-2; old 0..n_old-3 → new 0..n_new-2
            for i in range(n_old - 2):
                new_marks[i] = old_marks[i]
        else:
            # 中删：丢 old boundary idx-1 和 idx，添 new boundary idx-1 = None
            for i in range(idx - 1):
                new_marks[i] = old_marks[i]
            # new_marks[idx - 1] = None
            for i in range(idx + 1, n_old - 1):
                new_marks[i - 1] = old_marks[i]
        self.marked_frames = new_marks
        self.expected_marks = max(0, n_new - 1)
        self._save_edits_to_json()
        self._save_marks_to_json(silent=True)
        self._render_subtasks()
        self._refresh_marks_text(); self._refresh_status_text()

    def _edit_move(self, idx, delta):
        j = idx + delta
        if not (0 <= j < len(self.current_subtasks)): return
        self.current_subtasks[idx], self.current_subtasks[j] = self.current_subtasks[j], self.current_subtasks[idx]
        self.edit_log.append({"op": "move", "from": idx, "to": j})
        # 移动 idx ↔ idx+1（假设 |delta|=1）的 boundary 影响：
        # 涉及 boundary min(idx,j)-1, min(idx,j), max(idx,j) — 它们的相邻关系都变了
        lo = min(idx, j)
        affected = [lo - 1, lo, lo + 1] if abs(delta) == 1 else list(range(lo - 1, max(idx, j) + 1))
        for bi in affected:
            if 0 <= bi < len(self.marked_frames):
                self.marked_frames[bi] = None
        self._save_edits_to_json()
        self._save_marks_to_json(silent=True)
        self._render_subtasks()

    def _edit_revert_all(self):
        if not self.edit_log:
            messagebox.showinfo("Nothing to revert", ""); return
        if not messagebox.askyesno("Revert all edits?",
            f"丢弃 {len(self.edit_log)} 个编辑并还原原始 subtasks（marks 也清空）"):
            return
        self.current_subtasks = list(self.original_subtasks)
        self.edit_log = []
        self.expected_marks = max(0, len(self.current_subtasks) - 1)
        self.marked_frames = [None] * self.expected_marks
        ep = self.local_edits_path if self.folder_mode else edits_path(self.video_path)
        if os.path.exists(ep):
            try:
                os.remove(ep)
            except: pass
        self._save_marks_to_json(silent=True)
        self._render_subtasks()
        self._refresh_marks_text(); self._refresh_status_text()

    def _save_edits_to_json(self):
        if self.video_path is None: return
        ep = self.local_edits_path if self.folder_mode else edits_path(self.video_path)
        if not self.edit_log:
            if os.path.exists(ep):
                try:
                    os.remove(ep)
                except: pass
            return
        payload = {
            "task_name": self.task_name,
            "original_subtasks": self.original_subtasks,
            "edited_subtasks": self.current_subtasks,
            "edit_log": self.edit_log,
        }
        edit_dir = os.path.dirname(ep)
        if edit_dir:
            os.makedirs(edit_dir, exist_ok=True)
        with open(ep, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    # ---------- 状态 ----------

    def _refresh_status_text(self):
        if not self.video_path:
            self.info_label.config(text=""); return
        extra = ""
        if self.expected_marks > 0 and self.current_total_frames > 1:
            extra = f"   ·   max boundary {self._max_boundary_frame()}"
        self.info_label.config(
            text=f"frame {self.current_frame}   ·   {self._marked_count()}/{self.expected_marks} marks{extra}"
        )

    def _refresh_marks_text(self):
        pass  # marks 状态已通过 boundary 行可视化，不再单独显示

    # ---------- 视频 / seek ----------

    def _seek_to_frame(self, f, stop_playback=True):
        if self.cap is None: return
        if stop_playback:
            self.is_playing = False; self.play_pause_btn.config(text="▶  Play")
        max_frame = max(0, int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)) - 1)
        f = max(0, min(max_frame, f))
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ret, frame = self.cap.read()
        if ret:
            self.current_frame = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1
            self._show_frame(frame); self._set_scale_from_video(self.current_frame)
        self._refresh_status_text()

    def _on_seek_press(self, event):
        if self.cap is None: return
        self._was_playing_before_seek = self.is_playing
        self._seek_user_active = True
        self.is_playing = False; self.play_pause_btn.config(text="▶  Play")

    def _on_seek_release(self, event):
        if self.cap is None: return
        self._seek_user_active = False
        self._seek_to_frame(int(float(self.seek_scale.get())))
        if getattr(self, "_was_playing_before_seek", False):
            self.is_playing = True; self.play_pause_btn.config(text="⏸  Pause")

    def _on_seek_scale_change(self, val):
        if self.cap is None or self._scale_updating_from_video: return
        if not self._seek_user_active: return
        target = int(float(val))
        self._seek_to_frame(target)

    def _set_scale_from_video(self, frame_idx):
        self._scale_updating_from_video = True
        try: self.seek_scale.set(frame_idx)
        finally: self._scale_updating_from_video = False

    def toggle_play_pause(self):
        if self.cap is None: return
        self.is_playing = not self.is_playing
        self.play_pause_btn.config(text="⏸  Pause" if self.is_playing else "▶  Play")

    def step_back_frame(self):
        if self.cap is None: return
        self._seek_to_frame(self.current_frame - 1)

    def step_forward_frame(self):
        if self.cap is None: return
        self._seek_to_frame(self.current_frame + 1)

    def _on_m_key(self, event):
        fw = self.root.focus_get()
        for (_, w) in self._subtask_widgets:
            if fw == w: return
        self.mark_current_frame(); return "break"

    def _on_root_click(self, event):
        fw = self.root.focus_get()
        if fw is None: return
        for (_, w) in self._subtask_widgets:
            if fw == w:
                if event.widget != w: self.root.focus_set()
                break

    def mark_current_frame(self):
        if self.cap is None or self.current_frame < 0: return
        bi = self._next_pending_boundary()
        if bi is None: return
        if self.current_total_frames <= 1:
            messagebox.showwarning(
                "不能标记 boundary",
                "当前视频帧数不足，无法为多个 subtask 标注有效 boundary。"
            )
            return
        max_boundary = self._max_boundary_frame()
        if self.current_frame > max_boundary:
            messagebox.showwarning(
                "不能把 boundary 标在最后一帧",
                f"当前帧是 {self.current_frame}，最大允许 boundary 帧是 {max_boundary}。\n"
                "最后一个 subtask 的结束帧会自动设置为视频最后一帧，不需要额外标记。"
            )
            return
        self.marked_frames[bi] = self.current_frame
        self._refresh_status_text(); self._refresh_marks_text()
        self._render_subtasks()
        self._save_marks_to_json(silent=True)
        # 标完一个，自动滚到下一个待标行
        self.root.after(50, self._scroll_to_next_pending)

    def undo_mark(self):
        # 找最后一个已标的，清掉
        for i in range(len(self.marked_frames) - 1, -1, -1):
            if self.marked_frames[i] is not None:
                popped = self.marked_frames[i]
                self.marked_frames[i] = None
                self.saved_once = False
                self._refresh_status_text(); self._refresh_marks_text()
                self._render_subtasks()
                self._save_marks_to_json(silent=True)
                print(f"[undo] popped boundary {i} = frame {popped}")
                return

    def remark_current(self):
        mc = self._marked_count()
        if mc == 0 and self.current_frame == 0: return
        if not messagebox.askyesno("Re-mark", f"清空 {mc} 个 marks 并回到 frame 0？"):
            return
        self.marked_frames = [None] * self.expected_marks
        self.saved_once = False
        self._seek_to_frame(0)
        self._refresh_status_text(); self._refresh_marks_text()
        self._render_subtasks()
        self._save_marks_to_json(silent=True)

    def _validate_for_save(self):
        """返回 (ok, error_msg)。"""
        self._commit_all_subtask_edits()
        # 1) subtask 文本不能为空
        empty_idxs = [i for i, s in enumerate(self.current_subtasks) if not s.strip()]
        if empty_idxs:
            return False, f"以下 subtask 为空，先填好：{empty_idxs}"
        # 2) 所有 boundary 都标完
        if self.expected_marks == 0:
            return True, ""
        unmarked = [i for i, m in enumerate(self.marked_frames) if m is None]
        if unmarked:
            return False, f"还有 {len(unmarked)} 个 boundary 未标：{unmarked}"
        boundary_error = self._boundary_frame_error()
        if boundary_error:
            return False, boundary_error
        if any(int(self.marked_frames[i]) >= int(self.marked_frames[i + 1])
               for i in range(len(self.marked_frames) - 1)):
            return False, "boundary frames must be strictly increasing"
        return True, ""

    def next_episode(self):
        if not self.video_list or self.video_idx >= len(self.video_list): return
        ok, err = self._validate_for_save()
        if not ok:
            messagebox.showerror("还不能保存", err)
            return
        self.info_label.config(text="Saving JSON / rendering vis / uploading outputs ...")
        self.root.config(cursor="watch")
        for btn in (self.play_pause_btn, self.mark_btn, self.undo_btn,
                    self.rewind_btn, self.forward_btn, self.next_btn,
                    self.prev_btn, self.jump_unmarked_btn):
            btn.config(state=tk.DISABLED)
        self.root.update_idletasks()
        try:
            self._save_marks_to_json()
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            self._refresh_status_text()
            for btn in (self.play_pause_btn, self.mark_btn, self.undo_btn,
                        self.rewind_btn, self.forward_btn, self.next_btn,
                        self.prev_btn, self.jump_unmarked_btn):
                btn.config(state=tk.NORMAL)
            self.root.config(cursor="")
            return
        finally:
            self.root.config(cursor="")
        self._advance_to_next()

    def prev_episode(self):
        if self.video_idx == 0:
            messagebox.showinfo("First", ""); return
        self._commit_all_subtask_edits()
        # prev 不强制校验（用户可能想退回去看上一集），但若当前完整就先保存
        if not self.saved_once and self._marks_complete() \
                and all(s.strip() for s in self.current_subtasks):
            self._save_marks_to_json()
        self._load_video_at_idx(self.video_idx - 1)

    def jump_to_next_unmarked(self):
        for j, (n, p) in enumerate(self.video_list):
            if j <= self.video_idx: continue
            if not self._episode_is_complete(n, p):
                self._load_video_at_idx(j); return
        for j, (n, p) in enumerate(self.video_list):
            if not self._episode_is_complete(n, p):
                self._load_video_at_idx(j); return
        messagebox.showinfo("已全部完成", "所有集都已标完。")

    def jump_to_ep(self):
        val = self.jump_ep_var.get().strip()
        if not val: return
        try: target_n = int(val)
        except ValueError:
            messagebox.showerror("Bad input", ""); return
        for j, (n, _) in enumerate(self.video_list):
            if n == target_n:
                self._load_video_at_idx(j); return
        messagebox.showerror("Not found", f"No ep_{target_n}")

    # ---------- 视频循环 ----------

    def update_frame(self):
        if self.cap is not None and self.is_playing:
            ret, frame = self.cap.read()
            if ret:
                self.current_frame = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1
                self._show_frame(frame)
                self._set_scale_from_video(self.current_frame)
                self._refresh_status_text()
            else:
                self.is_playing = False; self.play_pause_btn.config(text="▶  Play")
                ok, err = self._validate_for_save()
                if ok:
                    messagebox.showinfo("Episode done",
                        f"全 {self.expected_marks} 个 boundary 已标，subtask 检查通过。\n"
                        "点 [Save & Next →] 保存最终 JSON / vis 并继续。")
                else:
                    messagebox.showwarning("还没法保存", err)
        self.root.after(self.frame_interval_ms, self.update_frame)

    def _show_frame(self, frame):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        img.thumbnail((VIDEO_MAX_W, VIDEO_MAX_H), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(img, master=self.root)
        self.video_label.config(image=photo)
        self.video_label.image = photo

    def _render_vis_video(self, episode_json):
        if not self.folder_mode or not self.output_vis_path or not self.local_video_path:
            return None
        if self.remote_mode and self.io.exists(self.output_vis_path):
            print(f"[skip existing vis] {self.output_vis_path}")
            return self.output_vis_path
        temp_dir = tempfile.mkdtemp(prefix="behavior_segment_vis_")
        local_json = os.path.join(temp_dir, path_basename(self.output_json_path))
        local_out = os.path.join(temp_dir, path_basename(self.output_vis_path))
        with open(local_json, "w", encoding="utf-8") as f:
            json.dump(episode_json, f, ensure_ascii=False, indent=2)
        render_segment_video(local_json, self.local_video_path, local_out)
        uploaded = self.io.upload_file(local_out, self.output_vis_path)
        if uploaded is False:
            print(f"[skip existing vis] {self.output_vis_path}")
        return self.output_vis_path

    def _save_marks_to_json(self, silent=False):
        if self.video_path is None: return
        if self.folder_mode:
            state_path = self.local_state_path
            self._write_local_json(state_path, self.marked_frames)
            self.saved_once = True
            if self._marks_complete():
                if silent:
                    return
                boundary_error = self._boundary_frame_error()
                if boundary_error:
                    if not silent:
                        messagebox.showerror("还不能保存", boundary_error)
                    return
                effective_meta = self._effective_task_meta()
                episode_json = build_human_episode_json(
                    self.template_json,
                    self.task_name,
                    effective_meta,
                    self.video_path,
                    self.marked_frames,
                    self.current_total_frames,
                    self.current_video_fps,
                )
                self._write_local_json(self.local_output_json_path, episode_json)
                if self.remote_mode:
                    uploaded_json = self.io.upload_file(self.local_output_json_path, self.output_json_path)
                    if uploaded_json is False:
                        print(f"[skip existing json] {self.output_json_path}")
                    if self.edit_log and self.local_edits_path and os.path.exists(self.local_edits_path):
                        uploaded_edits = self.io.upload_file(self.local_edits_path, self.output_edits_path)
                        if uploaded_edits is False:
                            print(f"[skip existing edits] {self.output_edits_path}")
                try:
                    vis_path = self._render_vis_video(episode_json)
                    if vis_path and not silent:
                        print(f"[saved vis] {vis_path}")
                except Exception as e:
                    print(f"[warn] failed to render vis video for {self.output_json_path}: {e}")
                self._remove_local_file(state_path)
                if not silent:
                    print(f"[saved] {self.output_json_path}")
            elif not silent:
                print(f"[saved progress] {state_path}")
            return
        bp = boundaries_path(self.video_path)
        # 保存时把 None 也存为 null，保留位置信息；同时仅写已标的元素也可
        # 这里选择存包含 null 的完整列表，外部消费者要识别
        with open(bp, "w", encoding="utf-8") as f:
            json.dump(self.marked_frames, f, ensure_ascii=False, indent=2)
        self.saved_once = True
        if not silent: print(f"[saved] {bp}")

    def _release_capture(self):
        if self.cap is not None:
            self.cap.release(); self.cap = None

    def on_close(self):
        self._commit_all_subtask_edits()
        if self.marked_frames and self.video_path:
            try: self._save_marks_to_json(silent=True)
            except: pass
        self._release_capture()
        self.root.destroy()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Video subtask boundary annotator. Without arguments, runs the legacy demo mode."
    )
    parser.add_argument("--root-dir", "--root_dir", dest="root_dir",
                        help="Dataset root. Looks for videos/task-XXXX/observation.images.rgb.head.")
    parser.add_argument("--task-id", "--task_id", dest="task_id", type=int,
                        help="Task index, e.g. 1 for task-0001.")
    parser.add_argument("--episode-range", "--episode_range",
                        "--episode-idx-range", "--episode_idx_range",
                        dest="episode_range",
                        help="Inclusive episode range. Use local ids like 0:10 or full ids like 10000:10010.")
    parser.add_argument("--video-dir", help="Legacy: folder containing videos to annotate; searched recursively.")
    parser.add_argument("--task", help="Legacy: task name in behavior_subtask_mapping.json, e.g. picking_up_trash.")
    parser.add_argument("--mapping", default=default_mapping_path(),
                        help="Local path to behavior_subtask_mapping.json. Defaults to this script folder.")
    parser.add_argument("--output-dir",
                        help="Output folder. Defaults to <video-dir>/human_anno.")
    parser.add_argument("--host", default="local",
                        help="local/localhost for local files, or an SSH host such as user@1.2.3.4 for server paths.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    root = tk.Tk()
    root.geometry("1400x820")
    io = make_io(args.host)

    if args.root_dir or args.task_id is not None:
        if not args.root_dir or args.task_id is None:
            messagebox.showerror("Error", "Use --root-dir and --task-id together.")
            sys.exit(1)
        if io.is_remote:
            root_dir = posixpath.normpath(args.root_dir)
            mapping_path = os.path.abspath(args.mapping)
        else:
            root_dir = os.path.abspath(args.root_dir)
            mapping_path = os.path.abspath(args.mapping)
        try:
            remote = io.is_remote
            task_name, task_meta = load_task_metadata_by_id(mapping_path, args.task_id, io=LocalIO())
            if remote:
                video_dir = default_task_video_dir(root_dir, args.task_id, remote=True)
                annotation_dir = None
            else:
                video_dir = resolve_task_video_dir(root_dir, args.task_id, io=io)
                annotation_dir = resolve_task_annotation_dir(root_dir, args.task_id, io=io)
            output_dir = args.output_dir
            if output_dir:
                output_dir = posixpath.normpath(output_dir) if remote else os.path.abspath(output_dir)
            else:
                output_dir = path_join(root_dir, "human_anno", task_folder_name(args.task_id), remote=remote)
        except Exception as e:
            messagebox.showerror("Error", str(e))
            sys.exit(1)
        try:
            if io.is_remote:
                videos = build_episode_paths_from_range(video_dir, args.task_id, args.episode_range, remote=True)
            else:
                videos = io.list_videos(video_dir)
                videos = filter_videos_by_episode_range(videos, args.task_id, args.episode_range)
        except Exception as e:
            messagebox.showerror("Error", f"Cannot prepare videos:\n{video_dir}\n\n{e}")
            sys.exit(1)
        if not videos:
            messagebox.showerror("Error", f"No videos found in:\n{video_dir}\nrange={args.episode_range}")
            sys.exit(1)
        print(f"Task: {task_name} (task_id={args.task_id})")
        print(f"Host: {args.host}")
        print(f"Root: {root_dir}")
        print(f"Video dir: {video_dir}")
        if annotation_dir:
            print(f"Template dir: {annotation_dir}")
        print(f"Mapping: {mapping_path}")
        print(f"Videos: {len(videos)} found")
        print(f"Output: {output_dir}")
        app = VideoMarkerApp(
            root,
            None,
            videos,
            task_name=task_name,
            task_meta=task_meta,
            input_dir=video_dir,
            output_dir=output_dir,
            io=io,
            host=args.host,
            template_dirs=[annotation_dir, video_dir, root_dir],
        )
    elif args.video_dir or args.task:
        if not args.video_dir or not args.task:
            messagebox.showerror("Error", "Use --video-dir and --task together.")
            sys.exit(1)
        if io.is_remote:
            video_dir = posixpath.normpath(args.video_dir)
            output_dir = posixpath.normpath(args.output_dir or posixpath.join(video_dir, "human_anno"))
            mapping_path = os.path.abspath(args.mapping)
        else:
            video_dir = os.path.abspath(args.video_dir)
            output_dir = os.path.abspath(args.output_dir or os.path.join(video_dir, "human_anno"))
            mapping_path = os.path.abspath(args.mapping)
        try:
            task_meta = load_task_metadata(mapping_path, args.task, io=LocalIO())
        except Exception as e:
            messagebox.showerror("Error", str(e))
            sys.exit(1)
        try:
            if io.is_remote:
                task_id = int(task_meta.get("task_index"))
                videos = build_episode_paths_from_range(video_dir, task_id, args.episode_range, remote=True)
            else:
                videos = io.list_videos(video_dir)
        except Exception as e:
            messagebox.showerror("Error", f"Cannot prepare videos:\n{video_dir}\n\n{e}")
            sys.exit(1)
        if not videos:
            messagebox.showerror("Error", f"No videos found in:\n{video_dir}")
            sys.exit(1)
        print(f"Task: {args.task}")
        print(f"Host: {args.host}")
        print(f"Mapping: {mapping_path}")
        print(f"Videos: {len(videos)} found")
        print(f"Output: {output_dir}")
        app = VideoMarkerApp(
            root,
            None,
            videos,
            task_name=args.task,
            task_meta=task_meta,
            input_dir=video_dir,
            output_dir=output_dir,
            io=io,
            host=args.host,
        )
    else:
        summary_json = find_raw_annotations_in_cwd()
        if summary_json is None:
            print(f"No *_raw_annotations.json in {os.getcwd()}", file=sys.stderr); sys.exit(1)
        videos = find_videos_in_cwd()
        if not videos:
            messagebox.showerror("Error", "No episode_N_annotation_check.mp4"); sys.exit(1)
        print(f"Annotations: {summary_json}")
        print(f"Videos: {len(videos)} found, ep {videos[0][0]}..{videos[-1][0]}")
        app = VideoMarkerApp(root, summary_json, videos)

    root.mainloop()

