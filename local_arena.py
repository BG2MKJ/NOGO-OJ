import argparse
import copy
import datetime
import json
import math
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Dict, List, Optional, Tuple


BOARD_SIZE = 9
BLACK = 1
WHITE = -1
DIR4 = [(-1, 0), (0, -1), (1, 0), (0, 1)]
DEFAULT_BOARD_COUNT = 4
DEFAULT_TIMEOUT = 2.0
REPLAY_INTERVAL_MS = 350
SCRIPT_REFRESH_MS = 1000
RUN_LAMP_BLINK_MS = 450


def empty_board():
    return [[0] * BOARD_SIZE for _ in range(BOARD_SIZE)]


class MoveRecord:
    def __init__(self, x, y, color, player_index):
        self.x = x
        self.y = y
        self.color = color
        self.player_index = player_index


class GameState:
    def __init__(self, game_id):
        self.game_id = game_id
        self.moves = []
        self.snapshots = [empty_board()]
        self.status = "等待开始"
        self.winner = None
        self.finished = False
        self.error = ""


def format_script_timestamp(path):
    try:
        stat = path.stat()
    except OSError:
        return "文件不存在"

    dt = datetime.datetime.fromtimestamp(stat.st_mtime)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_script_label(player_index, path_text):
    path = Path(path_text).expanduser()
    stamp = format_script_timestamp(path)
    return "玩家{0} 脚本: {1} | 更新时间: {2}".format(player_index, path.name or path_text, stamp)


class NoGoJudge:
    """Official judger.cpp logic ported line-by-line in behavior."""

    def __init__(self):
        self.grid_info = empty_board()
        self.dfs_air_visit = [[False] * BOARD_SIZE for _ in range(BOARD_SIZE)]

    @staticmethod
    def in_border(x, y):
        return 0 <= x < BOARD_SIZE and 0 <= y < BOARD_SIZE

    def dfs_air(self, fx, fy):
        self.dfs_air_visit[fx][fy] = True
        flag = False
        for dx, dy in DIR4:
            nx = fx + dx
            ny = fy + dy
            if self.in_border(nx, ny):
                if self.grid_info[nx][ny] == 0:
                    flag = True
                if self.grid_info[nx][ny] == self.grid_info[fx][fy] and not self.dfs_air_visit[nx][ny]:
                    if self.dfs_air(nx, ny):
                        flag = True
        return flag

    def judge_available(self, fx, fy, color):
        self.grid_info[fx][fy] = color
        self.dfs_air_visit = [[False] * BOARD_SIZE for _ in range(BOARD_SIZE)]
        if not self.dfs_air(fx, fy):
            self.grid_info[fx][fy] = 0
            return False
        for dx, dy in DIR4:
            nx = fx + dx
            ny = fy + dy
            if self.in_border(nx, ny):
                if self.grid_info[nx][ny] and not self.dfs_air_visit[nx][ny]:
                    if not self.dfs_air(nx, ny):
                        self.grid_info[fx][fy] = 0
                        return False
        self.grid_info[fx][fy] = 0
        return True

    def check_if_has_valid_move(self, color):
        for x in range(BOARD_SIZE):
            for y in range(BOARD_SIZE):
                if self.grid_info[x][y] == 0 and self.judge_available(x, y, color):
                    return False
        return True

    def proc_step(self, x, y, color, check_only=False):
        if not self.in_border(x, y) or self.grid_info[x][y]:
            return False
        if not self.judge_available(x, y, color):
            return False
        if not check_only:
            self.grid_info[x][y] = color
        return True


def build_payload(player_index, black_moves, white_moves):
    if player_index == 0:
        requests = [{"x": -1, "y": -1}]
        requests.extend({"x": x, "y": y} for x, y in white_moves)
        responses = [{"x": x, "y": y} for x, y in black_moves]
        return {"requests": requests, "responses": responses}

    requests = [{"x": x, "y": y} for x, y in black_moves]
    responses = [{"x": x, "y": y} for x, y in white_moves]
    return {"requests": requests, "responses": responses}


def parse_bot_output(stdout_text):
    lines = [line.strip() for line in stdout_text.splitlines() if line.strip()]
    if not lines:
        return None, None, "EMPTY_OUTPUT"

    raw = lines[-1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, None, f"INVALID_JSON_OUTPUT: {raw[:120]}"

    if isinstance(data, dict) and isinstance(data.get("response"), dict):
        content = data["response"]
    else:
        content = data

    if not isinstance(content, dict):
        return None, None, "INVALID_RESPONSE_SHAPE"

    x = content.get("x")
    y = content.get("y")
    if not isinstance(x, int) or isinstance(x, bool) or not isinstance(y, int) or isinstance(y, bool):
        return None, None, "MISSING_INT_XY"

    return x, y, None


def run_bot(bot_path, payload, timeout_sec, workspace_dir):
    if not bot_path.exists():
        return None, None, f"BOT_NOT_FOUND: {bot_path.name}"

    try:
        completed = subprocess.run(
            [sys.executable, str(bot_path)],
            input=json.dumps(payload, separators=(",", ":")),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            cwd=str(workspace_dir),
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, None, f"TIMEOUT>{timeout_sec:.2f}s"
    except OSError as exc:
        return None, None, f"SPAWN_ERROR: {exc}"

    if completed.returncode != 0:
        stderr = completed.stderr.strip().splitlines()
        tail = stderr[-1] if stderr else "non-zero exit"
        return None, None, f"RUNTIME_ERROR: {tail[:120]}"

    return parse_bot_output(completed.stdout)


def invalid_move_reason(judge, x, y):
    if judge.in_border(x, y) and judge.grid_info[x][y]:
        return f"INVALID_MOVE ({x},{y}) is not empty"
    return f"INVALID_MOVE ({x},{y}) is forbidden position"


def explain_invalid_move(judge, x, y, color):
    color_text = "黑方" if color == BLACK else "白方"

    if not judge.in_border(x, y):
        return "{0} 越界落子 ({1},{2})".format(color_text, x, y)

    if judge.grid_info[x][y] != 0:
        return "{0} 落子 ({1},{2}) 时该位置已有棋子".format(color_text, x, y)

    judge.grid_info[x][y] = color
    judge.dfs_air_visit = [[False] * BOARD_SIZE for _ in range(BOARD_SIZE)]
    has_self_air = judge.dfs_air(x, y)

    if not has_self_air:
        judge.grid_info[x][y] = 0
        return "{0} 落子 ({1},{2}) 属于自杀禁手".format(color_text, x, y)

    for dx, dy in DIR4:
        nx = x + dx
        ny = y + dy
        if judge.in_border(nx, ny):
            if judge.grid_info[nx][ny] and not judge.dfs_air_visit[nx][ny]:
                if not judge.dfs_air(nx, ny):
                    judge.grid_info[x][y] = 0
                    return "{0} 落子 ({1},{2}) 会导致相邻棋块无气，属于禁手".format(color_text, x, y)

    judge.grid_info[x][y] = 0
    return invalid_move_reason(judge, x, y)


class MatchWorker(threading.Thread):
    def __init__(
        self,
        generation,
        game_id,
        player0_path,
        player1_path,
        timeout_sec,
        workspace_dir,
        event_queue,
        stop_event,
    ):
        super().__init__(daemon=True)
        self.generation = generation
        self.game_id = game_id
        self.player_paths = [player0_path, player1_path]
        self.timeout_sec = timeout_sec
        self.workspace_dir = workspace_dir
        self.event_queue = event_queue
        self.stop_event = stop_event

    def emit(self, event_type, **payload):
        event = {
            "type": event_type,
            "generation": self.generation,
            "game_id": self.game_id,
        }
        event.update(payload)
        self.event_queue.put(event)

    def run(self):
        judge = NoGoJudge()
        black_moves = []
        white_moves = []
        current_color = BLACK
        move_index = 0

        self.emit("status", status="对局中")

        while not self.stop_event.is_set():
            player_index = 0 if current_color == BLACK else 1
            payload = build_payload(player_index, black_moves, white_moves)
            x, y, error = run_bot(
                bot_path=self.player_paths[player_index],
                payload=payload,
                timeout_sec=self.timeout_sec,
                workspace_dir=self.workspace_dir,
            )

            if self.stop_event.is_set():
                return

            if error is not None or x is None or y is None:
                winner = 1 - player_index
                loser_side = "黑方" if player_index == 0 else "白方"
                winner_side = "黑方" if winner == 0 else "白方"
                status = f"{loser_side} 输出无效，{winner_side} 胜: {error}"
                self.emit("finish", winner=winner, status=status, error=error or "UNKNOWN_ERROR")
                return

            if not judge.proc_step(x, y, current_color):
                winner = 1 - player_index
                loser_side = "黑方" if current_color == BLACK else "白方"
                winner_side = "黑方" if winner == 0 else "白方"
                reason = explain_invalid_move(judge, x, y, current_color)
                status = f"{loser_side} 非法落子，{winner_side} 胜: {reason}"
                self.emit("finish", winner=winner, status=status, error=reason)
                return

            if current_color == BLACK:
                black_moves.append((x, y))
            else:
                white_moves.append((x, y))

            move_index += 1
            snapshot = copy.deepcopy(judge.grid_info)
            self.emit(
                "move",
                x=x,
                y=y,
                color=current_color,
                player_index=player_index,
                move_index=move_index,
                snapshot=snapshot,
                status=f"第{move_index}手 玩家{player_index} 落子 ({x},{y})",
            )

            if judge.check_if_has_valid_move(-current_color):
                winner = player_index
                winner_side = "黑方" if current_color == BLACK else "白方"
                loser_side = "白方" if current_color == BLACK else "黑方"
                reason = "{0} 无合法步可下".format(loser_side)
                status = f"第{move_index}手后终局，{winner_side} 胜: {reason}"
                self.emit("finish", winner=winner, status=status, error=reason)
                return

            current_color *= -1


class BoardPanel:
    def __init__(self, parent, game_id):
        self.shell = tk.Frame(parent, bg="#f3efe7", bd=0, highlightthickness=0)
        self.frame = ttk.Frame(self.shell, padding=8, relief="ridge")
        self.base_title = "棋盘 {0}".format(game_id + 1)
        self.title_var = tk.StringVar(value=self.base_title)
        self.info_var = tk.StringVar(value="等待开始")
        self.mode_var = tk.StringVar(value="实时 0/0 手")

        title_row = ttk.Frame(self.frame)
        title_row.pack(anchor="w", fill="x")

        ttk.Label(title_row, textvariable=self.title_var, font=("Microsoft YaHei UI", 11, "bold")).pack(side="left", anchor="w")

        ttk.Label(self.frame, textvariable=self.info_var, wraplength=280, justify="left").pack(anchor="w", pady=(2, 4))

        self.canvas = tk.Canvas(self.frame, width=290, height=290, bg="#c89a4a", highlightthickness=0)
        self.canvas.pack(fill="both", expand=False)

        ttk.Label(self.frame, textvariable=self.mode_var).pack(anchor="w", pady=(4, 0))

        self.frame.grid(row=0, column=0, sticky="n")

        self.lamp_canvas = tk.Canvas(self.shell, width=72, height=380, highlightthickness=0, bg="#f3efe7", bd=0)
        self.lamp_canvas.grid(row=0, column=1, sticky="nsw")

        self.board_start = 32
        self.board_end = 258
        self.board_cell = float(self.board_end - self.board_start) / (BOARD_SIZE - 1)
        self.last_board_key = None
        self.last_lamp_key = None
        self.last_title_text = None
        self.last_status_text = None
        self.last_mode_text = None

        self.draw_board_base()

    def grid(self, row, column):
        self.shell.grid(row=row, column=column, padx=(8, 18), pady=8, sticky="n")

    def draw_board_base(self):
        self.canvas.delete("all")
        self.canvas.create_rectangle(18, 18, 272, 272, fill="#ddb66d", outline="#8d6730", width=2, tags="board_base")

        for i in range(BOARD_SIZE):
            coord = self.board_start + i * self.board_cell
            self.canvas.create_line(self.board_start, coord, self.board_end, coord, fill="#6b4f28", width=1.5, tags="board_base")
            self.canvas.create_line(coord, self.board_start, coord, self.board_end, fill="#6b4f28", width=1.5, tags="board_base")

    def render(
        self,
        snapshot,
        total_moves,
        display_step,
        status,
        finished,
        winner,
        replaying,
        last_move,
        blink_on,
    ):
        if finished and winner is not None:
            title_text = "{0} | 玩家{1} 胜".format(self.base_title, winner)
        else:
            title_text = self.base_title

        if title_text != self.last_title_text:
            self.title_var.set(title_text)
            self.last_title_text = title_text

        if status != self.last_status_text:
            self.info_var.set(status)
            self.last_status_text = status

        mode_name = "回放" if replaying else "实时"
        mode_text = "{0} {1}/{2} 手".format(mode_name, display_step, total_moves)
        if mode_text != self.last_mode_text:
            self.mode_var.set(mode_text)
            self.last_mode_text = mode_text

        self.render_board(snapshot, display_step, last_move)
        self.render_lamp(finished, winner, blink_on)

    def render_board(self, snapshot, display_step, last_move):
        snapshot_key = tuple(tuple(row) for row in snapshot)
        if last_move is not None and display_step > 0:
            highlight_key = (last_move.x, last_move.y)
        else:
            highlight_key = None

        board_key = (snapshot_key, highlight_key)
        if board_key == self.last_board_key:
            return

        self.canvas.delete("stones")
        self.canvas.delete("highlight")

        for x in range(BOARD_SIZE):
            for y in range(BOARD_SIZE):
                stone = snapshot[x][y]
                if stone == 0:
                    continue
                cx = self.board_start + x * self.board_cell
                cy = self.board_start + y * self.board_cell
                radius = self.board_cell * 0.37
                fill = "#111111" if stone == BLACK else "#f4f1ea"
                outline = "#000000" if stone == BLACK else "#999999"
                self.canvas.create_oval(
                    cx - radius,
                    cy - radius,
                    cx + radius,
                    cy + radius,
                    fill=fill,
                    outline=outline,
                    width=2,
                    tags="stones",
                )

        if highlight_key is not None:
            cx = self.board_start + last_move.x * self.board_cell
            cy = self.board_start + last_move.y * self.board_cell
            radius = self.board_cell * 0.45
            self.canvas.create_oval(
                cx - radius,
                cy - radius,
                cx + radius,
                cy + radius,
                outline="#d93636",
                width=2,
                tags="highlight",
            )

        self.last_board_key = board_key

    def render_lamp(self, finished, winner, blink_on):
        lamp_key = (finished, winner, blink_on if not finished else True)
        if lamp_key == self.last_lamp_key:
            return

        self.lamp_canvas.delete("all")
        mount_top = 184
        mount_bottom = 226
        pole_x = 33

        self.lamp_canvas.create_oval(12, mount_top + 18, 62, mount_bottom + 12, fill="#b9b2aa", outline="", stipple="gray50")
        self.lamp_canvas.create_rectangle(4, mount_top + 6, 28, mount_bottom + 2, fill="#7d858f", outline="#535962", width=2)
        self.lamp_canvas.create_rectangle(9, mount_top + 14, 20, mount_bottom - 6, fill="#aeb5bc", outline="#737b84", width=1)
        self.lamp_canvas.create_polygon(26, 196, 47, 191, 47, 219, 26, 214, fill="#8e969f", outline="#5d646c", width=2)
        self.lamp_canvas.create_line(28, 205, pole_x, 205, fill="#4f545b", width=4, capstyle=tk.ROUND)
        self.lamp_canvas.create_line(27, 207, pole_x, 207, fill="#9ea5ad", width=2, capstyle=tk.ROUND)
        self.lamp_canvas.create_line(pole_x + 4, 62, pole_x + 4, 204, fill="#7d8289", width=8, capstyle=tk.ROUND)
        self.lamp_canvas.create_line(pole_x + 1, 64, pole_x + 1, 204, fill="#40454b", width=2, capstyle=tk.ROUND)

        if not finished:
            if blink_on:
                glow_outer = "#fff1a8"
                glow_mid = "#ffe06b"
                glow_inner = "#f5cf3c"
                outline = "#8a6e0f"
            else:
                glow_outer = "#74682a"
                glow_mid = "#6b5b14"
                glow_inner = "#5c4d0c"
                outline = "#4a3d08"
        elif winner == 0:
            glow_outer = "#8fe0a6"
            glow_mid = "#45c96a"
            glow_inner = "#20a44c"
            outline = "#0f6a30"
        else:
            glow_outer = "#9dc8ff"
            glow_mid = "#5da2f0"
            glow_inner = "#2a7de1"
            outline = "#164d8c"

        self.lamp_canvas.create_line(pole_x + 4, 82, pole_x + 4, 30, fill=glow_outer, width=20, capstyle=tk.ROUND)
        self.lamp_canvas.create_line(pole_x + 4, 82, pole_x + 4, 30, fill=glow_mid, width=14, capstyle=tk.ROUND)
        self.lamp_canvas.create_line(pole_x + 4, 82, pole_x + 4, 30, fill=glow_inner, width=9, capstyle=tk.ROUND)
        self.lamp_canvas.create_line(pole_x + 4, 88, pole_x + 4, 24, fill=outline, width=2, capstyle=tk.ROUND)
        self.lamp_canvas.create_line(pole_x + 2, 78, pole_x + 2, 36, fill="#ffffff", width=1, capstyle=tk.ROUND)
        self.lamp_canvas.create_line(pole_x + 8, 84, pole_x + 8, 28, fill="#ffffff", width=1, capstyle=tk.ROUND)
        self.last_lamp_key = lamp_key


class ArenaApp:
    def __init__(self, root, workspace_dir, boards, timeout_sec):
        self.root = root
        self.workspace_dir = workspace_dir
        self.event_queue = queue.Queue()
        self.session_generation = 0
        self.stop_events = []
        self.workers = []
        self.game_states = []
        self.board_panels = []
        self.replay_active = False
        self.auto_replay = False
        self.replay_step = 0
        self.blink_on = True
        self.is_scrolling = False
        self.scroll_idle_after_id = None
        self.pending_render_game_ids = set()
        self.summary_dirty = False

        self.player0_var = tk.StringVar(value=str(workspace_dir / "player0-code.py"))
        self.player1_var = tk.StringVar(value=str(workspace_dir / "player1-code.py"))
        self.board_count_var = tk.IntVar(value=max(1, boards))
        self.timeout_var = tk.StringVar(value=f"{timeout_sec:.2f}")
        self.summary_var = tk.StringVar(value="准备开始")
        self.stats_var = tk.StringVar(value="玩家0胜局 0，玩家1胜局 0，玩家0胜率 --")
        self.player0_stamp_var = tk.StringVar(value="")
        self.player1_stamp_var = tk.StringVar(value="")
        self.lamp_legend_var = tk.StringVar(value="指示棒: 运行中黄闪 | 玩家0胜绿灯常亮 | 玩家1胜蓝灯常亮")

        self.root.title("NoGo 本地并行对局器")
        self.root.geometry("1300x800")
        self.root.minsize(1300, 800)
        self.root.maxsize(1300, 800)
        self.root.resizable(False, False)

        self.build_layout()
        self.refresh_script_metadata()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(30, self.poll_events)
        self.root.after(SCRIPT_REFRESH_MS, self.poll_script_metadata)
        self.root.after(RUN_LAMP_BLINK_MS, self.pulse_running_lamps)
        self.start_matches()

    def build_layout(self):
        controls = ttk.Frame(self.root, padding=10)
        controls.pack(fill="x")

        ttk.Label(controls, text="玩家0脚本").grid(row=0, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.player0_var, width=52).grid(row=0, column=1, sticky="ew", padx=(6, 12))
        ttk.Label(controls, text="玩家1脚本").grid(row=0, column=2, sticky="w")
        ttk.Entry(controls, textvariable=self.player1_var, width=52).grid(row=0, column=3, sticky="ew", padx=(6, 0))

        ttk.Label(controls, textvariable=self.player0_stamp_var, foreground="#2f4f4f").grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(controls, textvariable=self.player1_stamp_var, foreground="#2f4f4f").grid(row=1, column=2, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(controls, text="棋盘数").grid(row=2, column=0, sticky="w", pady=(8, 0))
        tk.Spinbox(controls, from_=1, to=64, textvariable=self.board_count_var, width=6).grid(row=2, column=1, sticky="w", padx=(6, 12), pady=(8, 0))
        ttk.Label(controls, text="单步超时(秒)").grid(row=2, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.timeout_var, width=8).grid(row=2, column=3, sticky="w", padx=(6, 0), pady=(8, 0))

        self.reset_button = ttk.Button(controls, text="全部重置重新下棋", command=self.start_matches)
        self.reset_button.grid(row=3, column=0, pady=(10, 0), sticky="w")

        self.replay_button = ttk.Button(controls, text="回放", command=self.toggle_replay)
        self.replay_button.grid(row=3, column=1, pady=(10, 0), sticky="w")

        ttk.Button(controls, text="上一个子", command=self.step_backward).grid(row=3, column=2, pady=(10, 0), sticky="w")
        ttk.Button(controls, text="下一个子", command=self.step_forward).grid(row=3, column=3, pady=(10, 0), sticky="w")

        ttk.Label(controls, textvariable=self.lamp_legend_var, foreground="#7a4b00").grid(row=4, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Label(controls, textvariable=self.stats_var, foreground="#0b6e4f").grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(controls, textvariable=self.summary_var, foreground="#1d3557").grid(row=6, column=0, columnspan=4, sticky="w", pady=(6, 0))

        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(3, weight=1)

        outer = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        outer.pack(fill="both", expand=True)

        self.scroll_canvas = tk.Canvas(outer, highlightthickness=0, bg="#f3efe7")
        self.scroll_canvas.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=self.on_scrollbar_command)
        scrollbar.pack(side="right", fill="y")
        self.scroll_canvas.configure(yscrollcommand=scrollbar.set)

        self.board_container = ttk.Frame(self.scroll_canvas)
        self.scroll_window = self.scroll_canvas.create_window((0, 0), window=self.board_container, anchor="nw")

        self.board_container.bind("<Configure>", self.on_board_container_configure)
        self.scroll_canvas.bind("<Configure>", self.on_canvas_configure)

    def on_board_container_configure(self, _event):
        self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

    def on_canvas_configure(self, event):
        self.scroll_canvas.itemconfigure(self.scroll_window, width=event.width)

    def on_scrollbar_command(self, *args):
        self.mark_scroll_active()
        self.scroll_canvas.yview(*args)

    def mark_scroll_active(self):
        self.is_scrolling = True
        if self.scroll_idle_after_id is not None:
            self.root.after_cancel(self.scroll_idle_after_id)
        self.scroll_idle_after_id = self.root.after(120, self.finish_scroll_activity)

    def finish_scroll_activity(self):
        self.scroll_idle_after_id = None
        self.is_scrolling = False
        self.flush_pending_renders()

    def flush_pending_renders(self):
        if self.pending_render_game_ids:
            pending_ids = sorted(self.pending_render_game_ids)
            self.pending_render_game_ids.clear()
            for game_id in pending_ids:
                if 0 <= game_id < len(self.game_states):
                    self.render_game(game_id)

        if self.summary_dirty:
            self.summary_dirty = False
            self.update_summary()

        if self.game_states:
            self.render_running_lamps()

    def stop_existing_workers(self):
        self.auto_replay = False
        for stop_event in self.stop_events:
            stop_event.set()
        self.stop_events = []
        self.workers = []

    def refresh_script_metadata(self):
        self.player0_stamp_var.set(format_script_label(0, self.player0_var.get()))
        self.player1_stamp_var.set(format_script_label(1, self.player1_var.get()))

    def poll_script_metadata(self):
        self.refresh_script_metadata()
        self.root.after(SCRIPT_REFRESH_MS, self.poll_script_metadata)

    def pulse_running_lamps(self):
        self.blink_on = not self.blink_on
        if self.game_states and not self.is_scrolling:
            self.render_running_lamps()
        self.root.after(RUN_LAMP_BLINK_MS, self.pulse_running_lamps)

    def parse_timeout(self):
        try:
            timeout = float(self.timeout_var.get())
            if timeout <= 0:
                raise ValueError
            return timeout
        except ValueError:
            self.timeout_var.set(f"{DEFAULT_TIMEOUT:.2f}")
            return DEFAULT_TIMEOUT

    def rebuild_boards(self, count):
        for child in self.board_container.winfo_children():
            child.destroy()

        self.board_panels = []
        columns = 3
        for game_id in range(count):
            panel = BoardPanel(self.board_container, game_id)
            row = game_id // columns
            column = game_id % columns
            panel.grid(row=row, column=column)
            self.board_panels.append(panel)

    def start_matches(self):
        self.stop_existing_workers()
        self.session_generation += 1
        self.replay_active = False
        self.replay_step = 0
        self.replay_button.config(text="回放")
        self.refresh_script_metadata()

        board_count = max(1, int(self.board_count_var.get()))
        timeout_sec = self.parse_timeout()
        self.game_states = [GameState(game_id=i) for i in range(board_count)]
        self.rebuild_boards(board_count)
        self.render_all()

        player0_path = Path(self.player0_var.get()).expanduser()
        player1_path = Path(self.player1_var.get()).expanduser()

        for game_id in range(board_count):
            stop_event = threading.Event()
            worker = MatchWorker(
                generation=self.session_generation,
                game_id=game_id,
                player0_path=player0_path,
                player1_path=player1_path,
                timeout_sec=timeout_sec,
                workspace_dir=self.workspace_dir,
                event_queue=self.event_queue,
                stop_event=stop_event,
            )
            self.stop_events.append(stop_event)
            self.workers.append(worker)
            worker.start()

        self.update_summary()

    def get_max_replay_step(self):
        if not self.game_states:
            return 0
        return max(len(state.snapshots) - 1 for state in self.game_states)

    def enter_replay_mode(self, initial_step=None):
        if initial_step is None:
            initial_step = self.get_max_replay_step()
        self.replay_active = True
        self.auto_replay = False
        self.replay_step = max(0, min(initial_step, self.get_max_replay_step()))
        self.replay_button.config(text="停止回放")
        self.render_all()
        self.update_summary()

    def exit_replay_mode(self):
        self.replay_active = False
        self.auto_replay = False
        self.replay_button.config(text="回放")
        self.render_all()
        self.update_summary()

    def toggle_replay(self):
        if self.replay_active:
            self.exit_replay_mode()
            return

        self.replay_active = True
        self.auto_replay = True
        self.replay_step = 0
        self.replay_button.config(text="停止回放")
        self.render_all()
        self.update_summary()
        self.root.after(REPLAY_INTERVAL_MS, self.advance_replay)

    def advance_replay(self):
        if not self.replay_active or not self.auto_replay:
            return

        max_step = self.get_max_replay_step()
        if self.replay_step >= max_step:
            self.auto_replay = False
            self.update_summary()
            return

        self.replay_step += 1
        self.render_all()
        self.update_summary()
        self.root.after(REPLAY_INTERVAL_MS, self.advance_replay)

    def step_backward(self):
        if not self.replay_active:
            self.enter_replay_mode(self.get_max_replay_step())
        self.auto_replay = False
        self.replay_step = max(0, self.replay_step - 1)
        self.render_all()
        self.update_summary()

    def step_forward(self):
        if not self.replay_active:
            self.enter_replay_mode(0)
        self.auto_replay = False
        self.replay_step = min(self.get_max_replay_step(), self.replay_step + 1)
        self.render_all()
        self.update_summary()

    def render_game(self, game_id):
        state = self.game_states[game_id]
        total_moves = len(state.snapshots) - 1
        if self.replay_active:
            display_step = min(self.replay_step, total_moves)
        else:
            display_step = total_moves

        snapshot = state.snapshots[display_step]
        last_move = state.moves[display_step - 1] if display_step > 0 else None
        self.board_panels[game_id].render(
            snapshot=snapshot,
            total_moves=total_moves,
            display_step=display_step,
            status=state.status,
            finished=state.finished,
            winner=state.winner,
            replaying=self.replay_active,
            last_move=last_move,
            blink_on=self.blink_on,
        )

    def render_all(self):
        for game_id in range(len(self.game_states)):
            self.render_game(game_id)

    def render_running_lamps(self):
        for game_id in range(len(self.game_states)):
            state = self.game_states[game_id]
            if not state.finished:
                self.board_panels[game_id].render_lamp(state.finished, state.winner, self.blink_on)

    def handle_event(self, event):
        if event.get("generation") != self.session_generation:
            return

        game_id = int(event["game_id"])
        state = self.game_states[game_id]
        event_type = event["type"]

        if event_type == "status":
            state.status = str(event["status"])
        elif event_type == "move":
            move = MoveRecord(
                x=int(event["x"]),
                y=int(event["y"]),
                color=int(event["color"]),
                player_index=int(event["player_index"]),
            )
            state.moves.append(move)
            state.snapshots.append(copy.deepcopy(event["snapshot"]))
            state.status = str(event["status"])
        elif event_type == "finish":
            state.finished = True
            state.winner = int(event["winner"])
            state.status = str(event["status"])
            state.error = str(event.get("error", ""))

        if self.is_scrolling:
            self.pending_render_game_ids.add(game_id)
            self.summary_dirty = True
            return

        self.render_game(game_id)

    def update_summary(self):
        total = len(self.game_states)
        finished = sum(1 for state in self.game_states if state.finished)
        live = total - finished
        player0_wins = sum(1 for state in self.game_states if state.finished and state.winner == 0)
        player1_wins = sum(1 for state in self.game_states if state.finished and state.winner == 1)

        if finished > 0:
            player0_rate = "{0:.1f}%".format(100.0 * player0_wins / finished)
        else:
            player0_rate = "--"

        self.stats_var.set(
            "玩家0胜局 {0}，玩家1胜局 {1}，玩家0胜率 {2}（按已结束局统计）".format(
                player0_wins,
                player1_wins,
                player0_rate,
            )
        )

        if self.replay_active:
            mode = "回放步数 {0}/{1}".format(self.replay_step, self.get_max_replay_step())
            if self.auto_replay:
                mode += "，自动播放中"
        else:
            mode = "实时模式"
        self.summary_var.set("总局数 {0}，进行中 {1}，已结束 {2}，{3}".format(total, live, finished, mode))

    def poll_events(self):
        processed = 0
        while processed < 300:
            try:
                event = self.event_queue.get_nowait()
            except queue.Empty:
                break
            self.handle_event(event)
            processed += 1

        if processed:
            if self.is_scrolling:
                self.summary_dirty = True
            else:
                self.update_summary()

        self.root.after(30, self.poll_events)

    def on_close(self):
        self.stop_existing_workers()
        self.root.destroy()


def parse_args():
    parser = argparse.ArgumentParser(description="NoGo 本地并行对局 GUI")
    parser.add_argument("--boards", type=int, default=DEFAULT_BOARD_COUNT, help="初始棋盘数量")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="单步超时秒数")
    return parser.parse_args()


def main():
    args = parse_args()
    workspace_dir = Path(__file__).resolve().parent
    root = tk.Tk()
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    ArenaApp(root, workspace_dir=workspace_dir, boards=max(1, args.boards), timeout_sec=max(0.1, args.timeout))
    root.mainloop()


if __name__ == "__main__":
    main()