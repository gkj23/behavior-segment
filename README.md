# Behavior Segment Annotation

Tk GUI for manually marking subtask boundary frames in BEHAVIOR videos. The tool reads task/subtask definitions from `behavior_subtask_mapping.json`, lets a human mark segment boundaries, then writes full episode-format segment JSON files and visualization videos.

## Install

```bash
pip install -r requirements.txt
```

`tkinter` is usually included in Windows conda/Python. On Linux, install Tk first if needed:

```bash
sudo apt install python3-tk
```

## Recommended Command

This example runs the local GUI while reading/writing data on `Guangdong4090`:

```powershell
D:\Downloads\miniconda\envs\handeye\python.exe D:\cur_semester\research\Behavior\BEHAVIOR-MEM\Behavior_segment\video_annotation_v12.py `
  --host user@ssh_ip `
  --root-dir /path/to/comet-1.5k `
  --task-id 1 `
  --episode-range 0:10
```

Equivalent one-line form(using Guangdong4090 as an example):

```powershell
D:\Downloads\miniconda\envs\handeye\python.exe D:\cur_semester\research\Behavior\BEHAVIOR-MEM\Behavior_segment\video_annotation_v12.py --host Guangdong4090 --root-dir /mnt/gaokejun/comet-1.5k --task-id 1 --episode-range 0:10
```

## Arguments

`--host`

SSH host used for remote files. Use `local` or omit it for local files. For the current server, use:

```text
Guangdong4090
```

In remote mode, the GUI runs locally. Startup does not open any SSH connection: mapping is read from the local file, and video paths are built from `--root-dir`, `--task-id`, and `--episode-range`. SSH is opened only when downloading the currently selected video or uploading final outputs. Per-frame marks and subtask edits are cached locally until the episode is complete, so pressing `M` or editing text does not repeatedly write to the server.

`--root-dir` / `--root_dir`

Dataset root directory. For Guangdong4090:

```text
/mnt/gaokejun/comet-1.5k
```

The script automatically looks for videos in:

```text
<root-dir>/videos/task-XXXX/observation.images.rgb.head
```

If that does not exist, it tries:

```text
<root-dir>/task-XXXX/observation.images.rgb.head
```

`--task-id` / `--task_id`

Numeric task index. The script uses this id to:

- find the video folder, for example `--task-id 1` -> `task-0001`
- find the task name in mapping JSON by matching `task_index`
- load the corresponding subtask list

Example:

```text
--task-id 1
```

maps to `picking_up_trash` in the current mapping.

`--episode-range` / `--episode_range` / `--episode-idx-range` / `--episode_idx_range`

Inclusive episode range to annotate. Two formats are supported:

```text
0:10
```

means local episode indices 0 through 10 for the selected task. For `task-id 1`, this covers:

```text
episode_00010000.mp4 ... episode_00010010.mp4
```

Full episode ids are also accepted:

```text
10000:10010
```

A single episode is also accepted:

```text
0
```

`--mapping`

Path to local `behavior_subtask_mapping.json`. If omitted, the default is the copy in this script folder:

```text
D:\cur_semester\research\Behavior\BEHAVIOR-MEM\Behavior_segment\behavior_subtask_mapping.json
```

In remote mode this is still a local path; the script does not SSH to read mapping at startup.

`--output-dir`

Optional output directory. If omitted in root/task-id mode, output defaults to:

```text
<root-dir>/human_anno/task-XXXX
```

For the recommended command above, default output is:

```text
/mnt/gaokejun/comet-1.5k/human_anno/task-0001
```

`--video-dir` and `--task`

Legacy mode. Use these only if videos are already in a task-specific folder and you want to pass the task name directly. For comet-style data, prefer `--root-dir` and `--task-id`.

## GUI Workflow

1. The right panel shows the ordered subtask list from mapping JSON.
2. Play or seek through the video.
3. Stop at the frame where one subtask ends and the next starts.
4. Press `M` or click the mark button.
5. Continue until all `N - 1` boundaries are marked for `N` subtasks.
6. Click save/next. The tool writes the final JSON and visualization video.

You may edit subtask text in the GUI. Edited names are written into both:

```text
subtasks[*].subtask
segments[*].subtask
```

## Output Structure

For:

```text
--root-dir /mnt/gaokejun/comet-1.5k
--task-id 1
```

the default output folder is:

```text
/mnt/gaokejun/comet-1.5k/human_anno/task-0001
```

After marking episodes 0 through 2, the output looks like:

```text
/mnt/gaokejun/comet-1.5k/human_anno/
+-- task-0001/
    +-- episode_00010000.json
    +-- episode_00010000_vis.mp4
    +-- episode_00010001.json
    +-- episode_00010001_vis.mp4
    +-- episode_00010002.json
    +-- episode_00010002_vis.mp4
```

If a user edits subtask text for an episode, an additional edit record is saved:

```text
episode_00010000_subtasks_edited.json
```

While an episode is incomplete, progress is temporarily stored as:

```text
episode_00010000_marks.json
```

In local mode this file is in the output folder. In remote mode it is kept in the local temp cache and is not uploaded; it is removed after the full episode JSON is saved and uploaded.

## Output Files

`episode_XXXXXXXX.json`

Full episode-format segment file. It includes:

- `task`
- `task_index`
- `task_prompt`
- `video_path`
- `total_frames`
- `fps`
- `subtasks`
- `segments`

The `segments[*].frame_range` values come from human-marked boundary frames.

`episode_XXXXXXXX_vis.mp4`

Visualization video rendered from the human segment JSON. It adds a black subtitle bar above the frame showing:

- task name
- current subtask
- frame index and timestamp

`episode_XXXXXXXX_subtasks_edited.json`

Only exists if subtask text was edited in the GUI. It records original subtasks, edited subtasks, and edit operations.

`episode_XXXXXXXX_marks.json`

Temporary progress file for unfinished annotation. Removed after successful final save.
