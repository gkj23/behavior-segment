#!/usr/bin/env python3
"""Render a video with subtitles from a human segment JSON."""

import argparse
import json
from pathlib import Path

import cv2
import numpy as np


FONT = cv2.FONT_HERSHEY_SIMPLEX


def executed_segments(data):
    segments = data.get("segments", []) or []
    return [seg for seg in segments if seg.get("frame_range")]


def segment_at(frame_idx, segments, pointer):
    while pointer < len(segments) - 1 and frame_idx > segments[pointer]["frame_range"][1]:
        pointer += 1
    if not segments:
        return pointer, None
    seg = segments[pointer]
    lo, hi = seg["frame_range"]
    if lo <= frame_idx <= hi:
        return pointer, seg
    return pointer, seg


def fit_text(text, max_w, font_scale, thickness):
    text = str(text or "")
    if not text:
        return ""
    if cv2.getTextSize(text, FONT, font_scale, thickness)[0][0] <= max_w:
        return text
    out = text
    while out and cv2.getTextSize(out + "...", FONT, font_scale, thickness)[0][0] > max_w:
        out = out[:-1]
    return out + "..." if out else "..."


def auto_bar_height(font_scale, thickness, n_lines=3):
    (_, h), baseline = cv2.getTextSize("Mg", FONT, font_scale, thickness)
    return (h + baseline + 6) * n_lines + 16


def draw_top_bar(frame, lines, bar_height, font_scale, thickness):
    height, width = frame.shape[:2]
    canvas = np.zeros((height + bar_height, width, 3), dtype=np.uint8)
    canvas[bar_height:, :, :] = frame
    (_, line_h), baseline = cv2.getTextSize("Mg", FONT, font_scale, thickness)
    y = 8 + line_h
    step = line_h + baseline + 6
    max_w = width - 20
    for line in lines:
        text = fit_text(line, max_w, font_scale, thickness)
        cv2.putText(canvas, text, (10, y), FONT, font_scale, (255, 255, 255), thickness, cv2.LINE_AA)
        y += step
    return canvas


def render_segment_video(
    segment_json,
    video_path,
    output_path,
    *,
    font_scale=0.7,
    thickness=1,
    bar_height=0,
    codec="mp4v",
):
    segment_json = Path(segment_json)
    video_path = Path(video_path)
    output_path = Path(output_path)

    data = json.loads(segment_json.read_text(encoding="utf-8"))
    segments = executed_segments(data)
    if not segments:
        raise ValueError(f"No segments with frame_range in {segment_json}")

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or data.get("fps") or 30.0)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or data.get("total_frames") or 0)
    bar_height = int(bar_height or auto_bar_height(font_scale, thickness, n_lines=3))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(output_path),
        cv2.VideoWriter_fourcc(*codec),
        fps,
        (width, height + bar_height),
    )
    if not writer.isOpened():
        cap.release()
        raise RuntimeError(f"Cannot open writer: {output_path}")

    task = data.get("task") or data.get("task_name") or ""
    pointer = 0
    frame_idx = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            pointer, seg = segment_at(frame_idx, segments, pointer)
            subtask = seg.get("subtask", "") if seg else ""
            t = frame_idx / fps if fps > 0 else 0.0
            lines = [
                f"Task: {task}",
                f"Subtask: {subtask}",
                f"Frame {frame_idx} / {max(0, total_frames - 1)}   Time {t:.2f} s",
            ]
            writer.write(draw_top_bar(frame, lines, bar_height, font_scale, thickness))
            frame_idx += 1
    finally:
        cap.release()
        writer.release()
    return output_path


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--segment-json", "--segment_json", required=True, help="Human segment JSON path.")
    parser.add_argument("--video", required=True, help="Source video path.")
    parser.add_argument("--output", required=True, help="Output visualization mp4 path.")
    parser.add_argument("--font-scale", "--font_scale", type=float, default=0.7)
    parser.add_argument("--thickness", type=int, default=1)
    parser.add_argument("--bar-height", "--bar_height", type=int, default=0)
    parser.add_argument("--codec", default="mp4v")
    return parser.parse_args()


def main():
    args = parse_args()
    out = render_segment_video(
        args.segment_json,
        args.video,
        args.output,
        font_scale=args.font_scale,
        thickness=args.thickness,
        bar_height=args.bar_height,
        codec=args.codec,
    )
    print(out)


if __name__ == "__main__":
    main()
