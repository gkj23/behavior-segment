# Behavior Segment Annotation

Tk-based GUI tool for manually annotating subtask boundary frames in BEHAVIOR-style videos.

The tool reads task/subtask definitions from `behavior_subtask_mapping.json`, lets a human mark the `N - 1` boundaries between `N` subtasks, and writes episode-format segment JSON files plus optional visualization videos with subtitles.

## Features

- Local GUI for frame-level subtask boundary annotation.
- Local dataset mode and SSH remote dataset mode.
- Task/subtask lookup by `task_index` from `behavior_subtask_mapping.json`.
- Editable subtask text in the GUI.
- Episode JSON output with human-marked `segments[*].frame_range`.
- Visualization video output with task/subtask subtitles.
- Remote mode avoids SSH at startup; SSH is used only when downloading a selected video or uploading final outputs.
- Remote outputs are not overwritten if the target file already exists.

## Install

Create or activate a Python environment, then install dependencies:

```bash
pip install -r requirements.txt
```

`tkinter` is required for the GUI. It is usually included with Windows Python/conda. On Linux, install it separately if needed:

```bash
sudo apt install python3-tk
```

## Smoke Test

This repository includes a small local example dataset under `video_example/`.

From the project folder, run:

```bash
python video_annotation_v12.py \
  --host local \
  --root-dir video_example \
  --task-id 1 \
  --episode-range 0:0
```

On Windows PowerShell:

```powershell
python .\video_annotation_v12.py `
  --host local `
  --root-dir .\video_example `
  --task-id 1 `
  --episode-range 0:0
```

The GUI should open one local example video for annotation. Final outputs are written under:

```text
video_example/human_anno/task-0001
```

## Expected Dataset Layout

For the recommended `--root-dir` / `--task-id` mode, the dataset should look like:

```text
<root-dir>/
+-- videos/
|   +-- task-0001/
|       +-- observation.images.rgb.head/
|           +-- episode_00010000.mp4
|           +-- episode_00010001.mp4
+-- human_anno/                  # created/used for outputs
```

For `task-id 1`, local episode index `0` maps to full episode id `10000`, so the expected filename is:

```text
episode_00010000.mp4
```

## Local Dataset Usage

Run from this folder:

```bash
python video_annotation_v12.py \
  --root-dir <local_dataset_root> \
  --task-id <task_index> \
  --episode-range <start:end>
```

Example:

```bash
python video_annotation_v12.py \
  --root-dir /data/comet-1.5k \
  --task-id 1 \
  --episode-range 0:10
```

On Windows PowerShell, use backticks for line continuation:

```powershell
python .\video_annotation_v12.py `
  --root-dir C:\path\to\comet-1.5k `
  --task-id 1 `
  --episode-range 0:10
```

## SSH Remote Dataset Usage

Remote mode runs the GUI locally, but reads videos and writes final outputs on an SSH server.

First make sure your SSH host works:

```bash
ssh <ssh_host> "echo ok"
```

Then run:

```bash
python video_annotation_v12.py \
  --host <ssh_host> \
  --root-dir <remote_dataset_root> \
  --task-id <task_index> \
  --episode-range <start:end>
```

Example:

```bash
python video_annotation_v12.py \
  --host my-server \
  --root-dir /data/comet-1.5k \
  --task-id 1 \
  --episode-range 0:10
```

In remote mode:

- `--root-dir` is a path on the SSH server.
- `--mapping` is still a local file path.
- Startup does not open SSH connections.
- Video paths are built from `--root-dir`, `--task-id`, and `--episode-range`.
- SSH is opened when loading a selected video.
- Final JSON/edit/visualization outputs are uploaded to the server.
- If a remote output file already exists, it is skipped instead of overwritten.

Remote mode requires `--episode-range`, because the script does not list videos on the server at startup.

## Arguments

`--host`

SSH host for remote mode. Omit it, or use `local`, for local files.

Examples:

```text
local
my-server
user@example.com
```

`--root-dir` / `--root_dir`

Dataset root directory. In local mode this is a local path. In remote mode this is a server path.

The script expects videos under:

```text
<root-dir>/videos/task-XXXX/observation.images.rgb.head
```

`--task-id` / `--task_id`

Numeric task index. The script uses it to:

- choose `task-XXXX`
- find the matching task in `behavior_subtask_mapping.json`
- load the subtask list

Example:

```text
--task-id 1
```

`--episode-range` / `--episode_range` / `--episode-idx-range` / `--episode_idx_range`

Inclusive episode range.

Local episode indices:

```text
0:10
```

For `--task-id 1`, this maps to:

```text
episode_00010000.mp4 ... episode_00010010.mp4
```

Full episode ids are also accepted:

```text
10000:10010
```

A single episode is accepted:

```text
0
```

`--mapping`

Local path to `behavior_subtask_mapping.json`.

If omitted, the script uses:

```text
./behavior_subtask_mapping.json
```

In remote mode this is still local. The script does not SSH to read mapping at startup.

`--output-dir`

Optional output directory.

If omitted in `--root-dir` / `--task-id` mode, the default is:

```text
<root-dir>/human_anno/task-XXXX
```

In local mode this is a local output path. In remote mode this is a server output path.

`--video-dir` and `--task`

Legacy mode for task-specific video folders. Prefer `--root-dir` and `--task-id` for BEHAVIOR/comet-style datasets.

## GUI Workflow

1. The right panel shows the ordered subtask list from mapping JSON.
2. Play or seek through the video.
3. Stop at the frame where one subtask ends and the next begins.
4. Press `M` or click the mark button.
5. Continue until all `N - 1` boundaries are marked for `N` subtasks.
6. Click `Save & Next` to write final outputs.

The last subtask automatically ends at the final video frame. Do not mark a boundary on the final frame; the GUI blocks this because it would leave the next segment with no valid frames.

Subtask text can be edited directly in the GUI. Edited names are written into both:

```text
subtasks[*].subtask
segments[*].subtask
```

## Output Structure

For:

```text
--root-dir <dataset_root>
--task-id 1
```

the default output folder is:

```text
<dataset_root>/human_anno/task-0001
```

After marking episodes 0 through 2:

```text
<dataset_root>/human_anno/
+-- task-0001/
    +-- episode_00010000.json
    +-- episode_00010000_vis.mp4
    +-- episode_00010001.json
    +-- episode_00010001_vis.mp4
    +-- episode_00010002.json
    +-- episode_00010002_vis.mp4
```

If subtask text was edited for an episode, an additional edit record is saved:

```text
episode_00010000_subtasks_edited.json
```

While an episode is incomplete, boundary progress is cached as:

```text
episode_00010000_marks.json
```

In local mode this temporary marks file is stored in the output folder. In remote mode it is kept in a local temporary cache and is not uploaded. It is removed after successful final save.

## Output Files

`episode_XXXXXXXX.json`

Full episode-format segment file. Important fields include:

- `task`
- `task_index`
- `task_prompt`
- `video_path`
- `total_frames`
- `fps`
- `subtasks`
- `segments`

`segments[*].frame_range` values come from human-marked boundary frames.

`episode_XXXXXXXX_vis.mp4`

Visualization video rendered from the human segment JSON. It adds a top subtitle bar with:

- task name
- current subtask
- frame index
- timestamp

`episode_XXXXXXXX_subtasks_edited.json`

Only exists if subtask text was edited in the GUI. It records original subtasks, edited subtasks, and edit operations.

`episode_XXXXXXXX_marks.json`

Temporary progress file for unfinished annotation. Removed after successful final save.

## Minimal SSH Video Download Test

Use this script to test whether the remote host can stream a video before running the GUI:

```bash
python ssh_video_download_mre.py \
  --host <ssh_host> \
  --remote-video <remote_video_path>
```

By default it downloads only the first 1 MB. To test the whole video:

```bash
python ssh_video_download_mre.py \
  --host <ssh_host> \
  --remote-video <remote_video_path> \
  --full
```

## Notes

- Do not commit generated videos, `__pycache__`, or temporary `.part` files.
- Keep `behavior_subtask_mapping.json` next to `video_annotation_v12.py`, or pass `--mapping`.
- In SSH mode, set up SSH keys or another non-interactive login method first.
