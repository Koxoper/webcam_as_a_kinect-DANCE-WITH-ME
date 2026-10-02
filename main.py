from __future__ import annotations

import argparse
import base64
import ctypes
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
os.environ.setdefault(
    "QT_LOGGING_RULES",
    ";".join(
        [
            "qt.multimedia.*=false",
            "qt.qpa.*=false",
            "qt.core.qobject.connect=false",
        ]
    ),
)

try:
    from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
    from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QImage, QLinearGradient, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QDoubleSpinBox,
        QFileDialog,
        QFrame,
        QGraphicsDropShadowEffect,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QListView,
        QListWidget,
        QListWidgetItem,
        QMessageBox,
        QPushButton,
        QSizePolicy,
        QSlider,
        QSpinBox,
        QStackedWidget,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ImportError:
    QObject = None
    QApplication = None

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
except ImportError:
    QAudioOutput = None
    QMediaPlayer = None
    QVideoSink = None

try:
    import cv2
except ImportError:
    print("Brakuje pakietu 'opencv-python'. Zainstaluj zaleznosci z requirements.txt.")
    sys.exit(1)

try:
    import mediapipe as mp
except ImportError:
    print("Brakuje pakietu 'mediapipe'. Zainstaluj zaleznosci z requirements.txt.")
    sys.exit(1)

try:
    import vlc
except ImportError:
    vlc = None

if not hasattr(mp, "solutions"):
    print(
        "Wykryto pakiet 'mediapipe', ale ta wersja nie udostepnia API 'mp.solutions'. "
        "Ten projekt wymaga klasycznej wersji MediaPipe zgodnej z interfejsem Pose."
    )
    print("Sprobuj: python -m pip install --upgrade --force-reinstall mediapipe==0.10.21")
    sys.exit(1)


POSE_LANDMARKS = mp.solutions.pose.PoseLandmark
IMPORTANT_LANDMARKS = [
    POSE_LANDMARKS.NOSE,
    POSE_LANDMARKS.LEFT_SHOULDER,
    POSE_LANDMARKS.RIGHT_SHOULDER,
    POSE_LANDMARKS.LEFT_ELBOW,
    POSE_LANDMARKS.RIGHT_ELBOW,
    POSE_LANDMARKS.LEFT_WRIST,
    POSE_LANDMARKS.RIGHT_WRIST,
    POSE_LANDMARKS.LEFT_HIP,
    POSE_LANDMARKS.RIGHT_HIP,
    POSE_LANDMARKS.LEFT_KNEE,
    POSE_LANDMARKS.RIGHT_KNEE,
    POSE_LANDMARKS.LEFT_ANKLE,
    POSE_LANDMARKS.RIGHT_ANKLE,
]
IMPORTANT_CONNECTIONS = [
    ("LEFT_SHOULDER", "RIGHT_SHOULDER"),
    ("LEFT_SHOULDER", "LEFT_ELBOW"),
    ("LEFT_ELBOW", "LEFT_WRIST"),
    ("RIGHT_SHOULDER", "RIGHT_ELBOW"),
    ("RIGHT_ELBOW", "RIGHT_WRIST"),
    ("LEFT_SHOULDER", "LEFT_HIP"),
    ("RIGHT_SHOULDER", "RIGHT_HIP"),
    ("LEFT_HIP", "RIGHT_HIP"),
    ("LEFT_HIP", "LEFT_KNEE"),
    ("LEFT_KNEE", "LEFT_ANKLE"),
    ("RIGHT_HIP", "RIGHT_KNEE"),
    ("RIGHT_KNEE", "RIGHT_ANKLE"),
]
ANGLE_TRIPLETS = [
    ("LEFT_SHOULDER", "LEFT_ELBOW", "LEFT_WRIST"),
    ("RIGHT_SHOULDER", "RIGHT_ELBOW", "RIGHT_WRIST"),
    ("LEFT_HIP", "LEFT_KNEE", "LEFT_ANKLE"),
    ("RIGHT_HIP", "RIGHT_KNEE", "RIGHT_ANKLE"),
]
SCORING_LANDMARK_WEIGHTS = {
    "NOSE": 0.65,
    "LEFT_ELBOW": 2.0,
    "RIGHT_ELBOW": 2.0,
    "LEFT_WRIST": 3.0,
    "RIGHT_WRIST": 3.0,
    "LEFT_KNEE": 1.35,
    "RIGHT_KNEE": 1.35,
    "LEFT_ANKLE": 1.55,
    "RIGHT_ANKLE": 1.55,
}
MOTION_TRACKED_LANDMARKS = ("LEFT_WRIST", "RIGHT_WRIST", "LEFT_ANKLE", "RIGHT_ANKLE", "NOSE")
DEFAULT_DIFFICULTY = "Standard"
BACKGROUND_MAX_FRAME_WIDTH = 1280
GAME_MAX_FRAME_WIDTH = 1280
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


@dataclass
class AppConfig:
    camera_index: int = 0
    width: int = 640
    height: int = 480
    camera_fps: int = 30
    prefer_mjpg: bool = True
    pose_model_complexity: int = 0
    min_detection_confidence: float = 0.5
    min_tracking_confidence: float = 0.5
    mirror_view: bool = True
    fullscreen: bool = True
    difficulty: str = DEFAULT_DIFFICULTY


@dataclass(frozen=True)
class DifficultyConfig:
    name: str
    distance_weight: float
    angle_weight: float
    visibility_bonus: float
    timing_window_seconds: float
    perfect_threshold: float
    good_threshold: float
    ok_threshold: float


DIFFICULTY_CONFIGS: dict[str, DifficultyConfig] = {
    DEFAULT_DIFFICULTY: DifficultyConfig(
        name=DEFAULT_DIFFICULTY,
        distance_weight=62.0,
        angle_weight=0.28,
        visibility_bonus=18.0,
        timing_window_seconds=0.95,
        perfect_threshold=80.0,
        good_threshold=66.0,
        ok_threshold=45.0,
    )
}


@dataclass
class PosePoint:
    x: float
    y: float
    visibility: float


@dataclass
class PoseFrame:
    timestamp: float
    points: dict[str, PosePoint]


@dataclass
class ChoreographyData:
    source_video: str
    fps: float
    duration_seconds: float
    frame_count: int
    frames: list[PoseFrame]


class DanceScorerGUI:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Dance with me")
        self.root.geometry("1280x800")
        self.root.minsize(1100, 720)
        self.root.configure(bg="#101820")
        self.fullscreen_menu = True
        self.root.attributes("-fullscreen", True)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda _event: self.set_menu_fullscreen(False))
        self.root.bind("<F11>", lambda _event: self.toggle_menu_fullscreen())

        self.video_path_var = tk.StringVar()
        self.output_path_var = tk.StringVar(value=str(Path("choreography.json").resolve()))
        self.choreography_path_var = tk.StringVar(value=str(Path("choreography.json").resolve()))
        self.scan_fps_var = tk.StringVar(value="10")
        self.camera_index_var = tk.StringVar(value="0")
        self.camera_choice_var = tk.StringVar()
        self.camera_choices: dict[str, int] = {}
        self.player_name_var = tk.StringVar(value="")
        self.mirror_view_var = tk.BooleanVar(value=True)
        self.menu_music_muted_var = tk.BooleanVar(value=False)
        self.difficulty_var = tk.StringVar(value=DEFAULT_DIFFICULTY)
        self.status_var = tk.StringVar(value="Wybierz film albo gotowa choreografie.")
        self.busy = False
        self.app_started = False
        self.account_names: list[str] = []
        self.selected_account_index = 0
        self.account_animation_after_id: str | None = None
        self.library_path = Path("choreography_library.json").resolve()
        self.leaderboard_path = Path("leaderboard.json").resolve()
        self.accounts_path = Path("player_accounts.json").resolve()
        self.hidden_choreographies_path = Path("hidden_choreographies.json").resolve()
        self.choreography_items: dict[str, dict[str, str]] = {}
        self.menu_preview_cap = None
        self.menu_preview_after_id: str | None = None
        self.menu_preview_image = None
        self.menu_preview_delay_ms = 33
        self.menu_preview_video_path: Path | None = None
        self.menu_audio_player: AudioPlayer | None = None
        self.scan_dialog: tk.Toplevel | None = None

        self._configure_styles()
        self._build_ui()
        self.refresh_accounts()
        self.refresh_camera_choices()
        self.refresh_choreography_library()
        self.refresh_leaderboard()

    def _configure_styles(self) -> None:
        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.option_add("*TCombobox*Listbox.background", "#0d141b")
        self.root.option_add("*TCombobox*Listbox.foreground", "#edf5f7")
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#24443f")
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#edf5f7")

        background = "#101820"
        panel = "#17232e"
        panel_alt = "#1f2d38"
        border = "#2f4354"
        text = "#edf5f7"
        muted = "#9fb2bf"
        accent = "#33c7a4"
        accent_hover = "#43d9b5"
        danger = "#ff6b6b"

        self.style.configure(".", font=("Segoe UI", 11), background=background, foreground=text)
        self.style.configure("App.TFrame", background=background)
        self.style.configure("Panel.TFrame", background=panel)
        self.style.configure("Preview.TFrame", background="#0b1117")
        self.style.configure("AccountTile.TFrame", background="#1b2b36", relief="solid", borderwidth=1)
        self.style.configure("Title.TLabel", background=background, foreground=text, font=("Segoe UI", 26, "bold"))
        self.style.configure("Subtitle.TLabel", background=background, foreground=muted, font=("Segoe UI", 12))
        self.style.configure("App.TLabel", background=background, foreground=text, font=("Segoe UI", 11))
        self.style.configure("Preview.TLabel", background="#0b1117", foreground=muted, font=("Segoe UI", 12))
        self.style.configure("AccountTile.TLabel", background="#1b2b36", foreground=text, font=("Segoe UI", 24, "bold"))
        self.style.configure("AccountHint.TLabel", background="#1b2b36", foreground=muted, font=("Segoe UI", 11))
        self.style.configure("Status.TLabel", background=panel, foreground=text, font=("Segoe UI", 11))

        self.style.configure(
            "Card.TLabelframe",
            background=panel,
            foreground=text,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            relief="solid",
        )
        self.style.configure(
            "Card.TLabelframe.Label",
            background=panel,
            foreground=text,
            font=("Segoe UI", 12, "bold"),
        )

        self.style.configure(
            "App.TButton",
            background=panel_alt,
            foreground=text,
            bordercolor=border,
            focusthickness=0,
            padding=(14, 9),
            font=("Segoe UI", 11, "bold"),
        )
        self.style.map(
            "App.TButton",
            background=[("active", "#2b3c49"), ("disabled", "#27333d")],
            foreground=[("disabled", "#70818c")],
        )
        self.style.configure(
            "Primary.TButton",
            background=accent,
            foreground="#06241d",
            bordercolor=accent,
            focusthickness=0,
            padding=(16, 10),
            font=("Segoe UI", 12, "bold"),
        )
        self.style.map("Primary.TButton", background=[("active", accent_hover), ("disabled", "#365b55")])
        self.style.configure(
            "Danger.TButton",
            background="#42242a",
            foreground="#ffd7dd",
            bordercolor="#5d3038",
            padding=(14, 9),
            font=("Segoe UI", 11, "bold"),
        )
        self.style.map("Danger.TButton", background=[("active", "#59303a")], foreground=[("active", "#ffffff")])

        self.style.configure(
            "App.TEntry",
            fieldbackground="#0d141b",
            foreground=text,
            insertcolor=text,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            padding=(8, 6),
        )
        self.style.configure(
            "App.TCombobox",
            fieldbackground="#0d141b",
            background=panel_alt,
            foreground=text,
            arrowcolor=text,
            bordercolor=border,
            padding=(8, 6),
        )
        self.style.map(
            "App.TCombobox",
            fieldbackground=[("readonly", "#0d141b"), ("focus", "#0d141b")],
            selectbackground=[("readonly", "#0d141b")],
            selectforeground=[("readonly", text)],
            background=[("active", "#223441"), ("readonly", panel_alt)],
            foreground=[("readonly", text)],
        )
        self.style.configure("App.TCheckbutton", background=background, foreground=text, font=("Segoe UI", 11))
        self.style.map("App.TCheckbutton", background=[("active", background)], foreground=[("active", text)])

        self.style.configure(
            "Treeview",
            background="#0d141b",
            foreground=text,
            fieldbackground="#0d141b",
            bordercolor=border,
            rowheight=30,
            font=("Segoe UI", 10),
        )
        self.style.configure(
            "Treeview.Heading",
            background=panel_alt,
            foreground=text,
            bordercolor=border,
            font=("Segoe UI", 10, "bold"),
            padding=(8, 6),
        )
        self.style.map(
            "Treeview",
            background=[("selected", "#24443f"), ("active", "#14232d")],
            foreground=[("selected", text), ("active", text)],
        )

    def _build_ui(self) -> None:
        self.root_container = ttk.Frame(self.root, padding=32, style="App.TFrame")
        self.root_container.pack(fill="both", expand=True)
        self.root_container.columnconfigure(0, weight=1)
        self.root_container.rowconfigure(0, weight=1)

        self.login_frame = ttk.Frame(self.root_container, style="App.TFrame")
        self.login_frame.grid(row=0, column=0, sticky="nsew")
        self.login_frame.columnconfigure(0, weight=1)
        self.login_frame.rowconfigure(1, weight=1)

        login_top = ttk.Frame(self.login_frame, style="App.TFrame")
        login_top.grid(row=0, column=0, sticky="new")
        login_top.columnconfigure(1, weight=1)
        ttk.Button(login_top, text="Dodaj konto", command=self.open_add_account_dialog, style="App.TButton").grid(
            row=0,
            column=0,
            sticky="w",
        )
        ttk.Button(login_top, text="Usuń konto", command=self.delete_selected_account, style="Danger.TButton").grid(
            row=0,
            column=2,
            sticky="e",
        )
        ttk.Button(login_top, text="Wyjdź z gry", command=self.close, style="Danger.TButton").grid(
            row=0,
            column=3,
            sticky="e",
            padx=(10, 0),
        )

        login_card = ttk.Frame(self.login_frame, padding=34, style="Panel.TFrame")
        login_card.grid(row=1, column=0)
        login_card.columnconfigure(0, weight=1)

        ttk.Label(login_card, text="Dance with me", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            login_card,
            text="Wybierz konto, a potem przejdź do gry.",
            style="Subtitle.TLabel",
            wraplength=520,
        ).grid(row=1, column=0, sticky="w", pady=(8, 24))

        carousel = ttk.Frame(login_card, style="Panel.TFrame")
        carousel.grid(row=2, column=0, sticky="ew")
        carousel.columnconfigure(1, weight=1)
        ttk.Button(carousel, text="<", command=self.previous_account, style="App.TButton").grid(
            row=0,
            column=0,
            sticky="ns",
            padx=(0, 14),
        )
        self.account_canvas = tk.Canvas(
            carousel,
            width=560,
            height=250,
            bg="#17232e",
            highlightthickness=0,
            bd=0,
        )
        self.account_canvas.grid(row=0, column=1, sticky="ew")
        self.account_canvas.bind("<Button-1>", lambda _event: self.enter_app())
        self.account_canvas.bind("<Configure>", lambda _event: self.update_account_tile())
        ttk.Button(carousel, text=">", command=self.next_account, style="App.TButton").grid(
            row=0,
            column=2,
            sticky="ns",
            padx=(14, 0),
        )

        ttk.Button(login_card, text="Graj", command=self.enter_app, style="Primary.TButton").grid(
            row=3,
            column=0,
            sticky="ew",
            pady=(28, 0),
        )

        self.app_frame = ttk.Frame(self.root_container, style="App.TFrame")
        self.app_frame.columnconfigure(0, weight=3)
        self.app_frame.columnconfigure(1, weight=2)
        self.app_frame.rowconfigure(2, weight=1)

        topbar = ttk.Frame(self.app_frame, style="App.TFrame")
        topbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 22))
        topbar.columnconfigure(0, weight=1)
        ttk.Label(topbar, text="Dance with me", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(topbar, text="Zmień konto", command=self.change_account, style="App.TButton").grid(
            row=0,
            column=1,
            sticky="e",
            padx=(0, 14),
        )
        self.player_badge_label = ttk.Label(topbar, text="", style="Subtitle.TLabel")
        self.player_badge_label.grid(row=0, column=2, sticky="e")

        controls = ttk.Frame(self.app_frame, style="Panel.TFrame", padding=18)
        controls.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 18))
        controls.columnconfigure((0, 1, 2, 3, 4, 5), weight=1)

        ttk.Label(controls, text="Kamerka", style="Status.TLabel").grid(row=0, column=0, sticky="w")
        self.camera_combobox = ttk.Combobox(
            controls,
            textvariable=self.camera_choice_var,
            values=[],
            state="readonly",
            width=24,
            style="App.TCombobox",
        )
        self.camera_combobox.grid(row=1, column=0, sticky="ew", padx=(0, 12))
        self.camera_combobox.bind("<<ComboboxSelected>>", self.on_camera_selected)
        ttk.Checkbutton(
            controls,
            text="Odbicie lustrzane",
            variable=self.mirror_view_var,
            style="App.TCheckbutton",
        ).grid(row=1, column=1, sticky="w", padx=(0, 12))

        self.preview_button = ttk.Button(controls, text="Podgląd kamerki", command=self.start_camera_preview, style="App.TButton")
        self.preview_button.grid(row=1, column=2, sticky="ew", padx=(0, 12))
        self.scan_button = ttk.Button(controls, text="Skanuj choreografię", command=self.open_scan_dialog, style="App.TButton")
        self.scan_button.grid(row=1, column=3, sticky="ew", padx=(0, 12))
        self.play_button = ttk.Button(controls, text="Graj", command=self.start_play, style="Primary.TButton")
        self.play_button.grid(row=1, column=4, sticky="ew")

        library_frame = ttk.LabelFrame(self.app_frame, text="Choreografie", padding=14, style="Card.TLabelframe")
        library_frame.grid(row=2, column=0, sticky="nsew", padx=(0, 18))
        library_frame.columnconfigure(0, weight=1)
        library_frame.rowconfigure(0, weight=1)

        self.choreography_tree = ttk.Treeview(
            library_frame,
            columns=("title", "duration", "frames"),
            show="headings",
            height=12,
            selectmode="browse",
        )
        self.choreography_tree.heading("title", text="Piosenka")
        self.choreography_tree.heading("duration", text="Czas")
        self.choreography_tree.heading("frames", text="Pozy")
        self.choreography_tree.column("title", width=430, stretch=True)
        self.choreography_tree.column("duration", width=90, anchor="center", stretch=False)
        self.choreography_tree.column("frames", width=80, anchor="center", stretch=False)
        self.choreography_tree.grid(row=0, column=0, sticky="nsew")
        self.choreography_tree.bind("<<TreeviewSelect>>", self.on_choreography_selected)

        library_buttons = ttk.Frame(library_frame, style="Panel.TFrame")
        library_buttons.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(library_buttons, text="Dodaj choreografię", command=self.add_choreography_to_library, style="App.TButton").pack(
            side="left",
            padx=(0, 8),
        )
        ttk.Button(library_buttons, text="Odśwież listę", command=self.refresh_choreography_library, style="App.TButton").pack(
            side="left",
            padx=(0, 8),
        )
        self.edit_button = ttk.Button(library_buttons, text="Edytuj szkielet", command=self.start_edit_choreography, style="App.TButton")
        self.edit_button.pack(side="left", padx=(0, 8))
        self.menu_music_button = ttk.Button(library_buttons, text="Wycisz muzykę", command=self.toggle_menu_music, style="App.TButton")
        self.menu_music_button.pack(side="left")

        right_column = ttk.Frame(self.app_frame, style="App.TFrame")
        right_column.grid(row=2, column=1, sticky="nsew")
        right_column.columnconfigure(0, weight=1)
        right_column.rowconfigure(1, weight=1)

        preview_panel = ttk.LabelFrame(right_column, text="Podgląd", padding=14, style="Card.TLabelframe")
        preview_panel.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        preview_panel.columnconfigure(0, weight=1)
        preview_box = ttk.Frame(preview_panel, width=420, height=236, style="Preview.TFrame")
        preview_box.grid(row=0, column=0, sticky="ew")
        preview_box.grid_propagate(False)
        self.menu_preview_label = ttk.Label(preview_box, text="Wybierz choreografię", anchor="center", style="Preview.TLabel")
        self.menu_preview_label.pack(fill="both", expand=True)

        leaderboard_frame = ttk.LabelFrame(right_column, text="Tabela wyników", padding=14, style="Card.TLabelframe")
        leaderboard_frame.grid(row=1, column=0, sticky="nsew")
        leaderboard_frame.columnconfigure(0, weight=1)
        leaderboard_frame.rowconfigure(0, weight=1)
        self.leaderboard_tree = ttk.Treeview(
            leaderboard_frame,
            columns=("player", "song", "score", "grade", "difficulty", "date"),
            show="headings",
            height=8,
        )
        self.leaderboard_tree.heading("player", text="Gracz")
        self.leaderboard_tree.heading("song", text="Piosenka")
        self.leaderboard_tree.heading("score", text="Wynik")
        self.leaderboard_tree.heading("grade", text="Ocena")
        self.leaderboard_tree.heading("difficulty", text="Tryb")
        self.leaderboard_tree.heading("date", text="Data")
        self.leaderboard_tree.column("player", width=110, stretch=False)
        self.leaderboard_tree.column("song", width=220, stretch=True)
        self.leaderboard_tree.column("score", width=70, anchor="center", stretch=False)
        self.leaderboard_tree.column("grade", width=60, anchor="center", stretch=False)
        self.leaderboard_tree.column("difficulty", width=90, anchor="center", stretch=False)
        self.leaderboard_tree.column("date", width=130, anchor="center", stretch=False)
        self.leaderboard_tree.grid(row=0, column=0, sticky="nsew")

        bottom_bar = ttk.Frame(self.app_frame, style="App.TFrame")
        bottom_bar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        bottom_bar.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(bottom_bar, textvariable=self.status_var, wraplength=900, justify="left", style="Subtitle.TLabel")
        self.status_label.grid(row=0, column=0, sticky="w")
        self.exit_button = ttk.Button(bottom_bar, text="Zamknij", command=self.close, style="Danger.TButton")
        self.exit_button.grid(row=0, column=1, sticky="e")

    def set_menu_fullscreen(self, fullscreen: bool) -> None:
        self.fullscreen_menu = fullscreen
        self.root.attributes("-fullscreen", fullscreen)
        if not fullscreen:
            self.root.geometry("1280x800")

    def toggle_menu_fullscreen(self) -> None:
        self.set_menu_fullscreen(not self.fullscreen_menu)

    def enter_app(self) -> None:
        if self.account_names:
            self.selected_account_index %= len(self.account_names)
            self.player_name_var.set(self.account_names[self.selected_account_index])

        player_name = self.player_name_var.get().strip()
        if not player_name:
            messagebox.showerror("Brak konta", "Dodaj albo wybierz konto przed startem.")
            return
        self.player_name_var.set(player_name)
        self.player_badge_label.config(text=f"Grasz jako: {player_name}")
        self.login_frame.grid_remove()
        self.app_frame.grid(row=0, column=0, sticky="nsew")
        self.app_started = True
        self.on_choreography_selected()
        self.status_var.set("Wybierz choreografie i zacznij gre.")

    def change_account(self) -> None:
        self._stop_menu_preview()
        self.app_started = False
        self.app_frame.grid_remove()
        self.login_frame.grid(row=0, column=0, sticky="nsew")
        self.refresh_accounts()
        self.status_var.set("Wybierz konto.")

    def refresh_camera_choices(self) -> None:
        camera_options = list_available_cameras()
        self.camera_choices = {label: index for label, index in camera_options}
        labels = list(self.camera_choices)

        if not labels:
            labels = ["Kamera 0"]
            self.camera_choices = {"Kamera 0": 0}

        if hasattr(self, "camera_combobox"):
            self.camera_combobox.configure(values=labels)

        current_index = self.camera_index_var.get().strip()
        selected_label = next(
            (label for label, index in self.camera_choices.items() if str(index) == current_index),
            labels[0],
        )
        self.camera_choice_var.set(selected_label)
        self.camera_index_var.set(str(self.camera_choices[selected_label]))

    def on_camera_selected(self, _event=None) -> None:
        selected_label = self.camera_choice_var.get()
        camera_index = self.camera_choices.get(selected_label)
        if camera_index is not None:
            self.camera_index_var.set(str(camera_index))

    def get_selected_camera_index(self) -> int:
        selected_label = self.camera_choice_var.get()
        if selected_label in self.camera_choices:
            camera_index = self.camera_choices[selected_label]
            self.camera_index_var.set(str(camera_index))
            return camera_index
        return int(self.camera_index_var.get().strip())

    def _load_accounts(self) -> list[str]:
        if not self.accounts_path.exists():
            return []
        try:
            payload = json.loads(self.accounts_path.read_text(encoding="utf-8"))
        except Exception:
            return []

        raw_accounts = payload.get("accounts", []) if isinstance(payload, dict) else payload
        if not isinstance(raw_accounts, list):
            return []

        names: list[str] = []
        for item in raw_accounts:
            if isinstance(item, dict):
                name = item.get("name")
            else:
                name = item
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        return sorted(set(names), key=str.lower)

    def _save_accounts(self, names: list[str]) -> None:
        unique_names = sorted({name.strip() for name in names if name.strip()}, key=str.lower)
        payload = {"accounts": [{"name": name} for name in unique_names]}
        self.accounts_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def remember_account(self, name: str) -> None:
        clean_name = name.strip()
        if not clean_name:
            return
        names = self._load_accounts()
        if clean_name not in names:
            names.append(clean_name)
            self._save_accounts(names)
            self.refresh_accounts()

    def refresh_accounts(self) -> None:
        self.account_names = self._load_accounts()
        if self.selected_account_index >= len(self.account_names):
            self.selected_account_index = max(0, len(self.account_names) - 1)
        self.update_account_tile()

    def update_account_tile(self) -> None:
        if not hasattr(self, "account_canvas"):
            return
        self.draw_account_carousel(offset=0)

        if not self.account_names:
            self.player_name_var.set("")
            return

        self.selected_account_index %= len(self.account_names)
        selected_name = self.account_names[self.selected_account_index]
        self.player_name_var.set(selected_name)

    def draw_account_carousel(self, offset: float = 0.0) -> None:
        self.account_canvas.delete("all")
        width = int(self.account_canvas.winfo_width() or 560)
        height = int(self.account_canvas.winfo_height() or 250)
        center_x = width / 2
        center_y = height / 2

        if not self.account_names:
            self.draw_account_card(
                center_x,
                center_y,
                340,
                170,
                "Brak kont",
                "Dodaj konto w lewym górnym rogu",
                active=True,
            )
            return

        gap = 250
        card_specs = [
            (-1, center_x - gap + offset, 210, 140, False),
            (0, center_x + offset, 340, 180, True),
            (1, center_x + gap + offset, 210, 140, False),
        ]
        for relative_index, x, card_width, card_height, active in card_specs:
            name = self.account_names[(self.selected_account_index + relative_index) % len(self.account_names)]
            subtitle = (
                f"{self.selected_account_index + 1} / {len(self.account_names)}"
                if active
                else "konto"
            )
            self.draw_account_card(x, center_y, card_width, card_height, name, subtitle, active)

    def draw_account_card(
        self,
        center_x: float,
        center_y: float,
        width: int,
        height: int,
        name: str,
        subtitle: str,
        active: bool,
    ) -> None:
        x1 = center_x - width / 2
        y1 = center_y - height / 2
        x2 = center_x + width / 2
        y2 = center_y + height / 2
        fill = "#1f3440" if active else "#14232d"
        outline = "#33c7a4" if active else "#2f4354"
        text = "#edf5f7" if active else "#9fb2bf"
        subtitle_color = "#9fb2bf" if active else "#60717c"
        self.account_canvas.create_rectangle(x1 + 8, y1 + 10, x2 + 8, y2 + 10, fill="#0b1117", outline="")
        self.account_canvas.create_rectangle(x1, y1, x2, y2, fill=fill, outline=outline, width=2)
        self.account_canvas.create_text(
            center_x,
            center_y - 12,
            text=name,
            fill=text,
            font=("Segoe UI", 24 if active else 15, "bold"),
            width=width - 32,
        )
        self.account_canvas.create_text(
            center_x,
            center_y + 38,
            text=subtitle,
            fill=subtitle_color,
            font=("Segoe UI", 11),
            width=width - 32,
        )

    def animate_account_carousel(self, direction: int) -> None:
        if len(self.account_names) <= 1 or self.account_animation_after_id is not None:
            return

        gap = 250
        frames = 12
        target_offset = -gap if direction > 0 else gap

        def step(frame_index: int) -> None:
            progress = frame_index / frames
            eased = 1 - ((1 - progress) ** 3)
            self.draw_account_carousel(offset=target_offset * eased)
            if frame_index < frames:
                self.account_animation_after_id = self.root.after(16, lambda: step(frame_index + 1))
                return

            self.account_animation_after_id = None
            self.selected_account_index = (self.selected_account_index + direction) % len(self.account_names)
            self.update_account_tile()

        step(1)

    def previous_account(self) -> None:
        if not self.account_names:
            return
        self.animate_account_carousel(-1)

    def next_account(self) -> None:
        if not self.account_names:
            return
        self.animate_account_carousel(1)

    def open_add_account_dialog(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Dodaj konto")
        dialog.configure(bg="#101820")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("460x220")
        dialog.resizable(False, False)

        name_var = tk.StringVar()
        frame = ttk.Frame(dialog, padding=22, style="App.TFrame")
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text="Dodaj konto", style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 18))
        ttk.Label(frame, text="Nazwa użytkownika", style="App.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 6))
        entry = ttk.Entry(frame, textvariable=name_var, style="App.TEntry")
        entry.grid(row=2, column=0, sticky="ew")

        def submit() -> None:
            self.add_account(name_var.get())
            if name_var.get().strip():
                dialog.grab_release()
                dialog.destroy()

        entry.bind("<Return>", lambda _event: submit())
        ttk.Button(frame, text="Dodaj konto", command=submit, style="Primary.TButton").grid(
            row=3,
            column=0,
            sticky="ew",
            pady=(20, 0),
        )
        entry.focus_set()

    def add_account(self, name: str) -> None:
        player_name = name.strip()
        if not player_name:
            messagebox.showerror("Brak nazwy", "Wpisz nazwe konta do dodania.")
            return
        self.remember_account(player_name)
        self.account_names = self._load_accounts()
        self.selected_account_index = self.account_names.index(player_name)
        self.update_account_tile()
        self.status_var.set(f"Dodano konto: {player_name}")

    def delete_selected_account(self) -> None:
        if not self.account_names:
            return
        self.selected_account_index %= len(self.account_names)
        removed_name = self.account_names[self.selected_account_index]
        remaining_names = [name for name in self.account_names if name != removed_name]
        self._save_accounts(remaining_names)
        self.account_names = remaining_names
        if self.selected_account_index >= len(self.account_names):
            self.selected_account_index = max(0, len(self.account_names) - 1)
        self.update_account_tile()
        self.status_var.set(f"Usunięto konto: {removed_name}")

    def open_scan_dialog(self) -> None:
        if self.busy:
            return
        if self.scan_dialog is not None and self.scan_dialog.winfo_exists():
            self.scan_dialog.lift()
            return

        self.scan_dialog = tk.Toplevel(self.root)
        self.scan_dialog.title("Skanuj choreografie")
        self.scan_dialog.configure(bg="#101820")
        self.scan_dialog.transient(self.root)
        self.scan_dialog.grab_set()
        self.scan_dialog.geometry("760x330")
        self.scan_dialog.minsize(680, 300)
        self.scan_dialog.protocol("WM_DELETE_WINDOW", self.close_scan_dialog)

        frame = ttk.Frame(self.scan_dialog, padding=22, style="App.TFrame")
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Skanuj choreografie", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 18))

        ttk.Label(frame, text="Film do skanowania", style="App.TLabel").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.video_path_var, style="App.TEntry").grid(row=1, column=1, sticky="ew", padx=10, pady=6)
        ttk.Button(frame, text="Wybierz film", command=self.pick_video, style="App.TButton").grid(row=1, column=2, sticky="ew", pady=6)

        ttk.Label(frame, text="Plik wyjsciowy JSON", style="App.TLabel").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.output_path_var, style="App.TEntry").grid(row=2, column=1, sticky="ew", padx=10, pady=6)
        ttk.Button(frame, text="Zapisz jako", command=self.pick_output, style="App.TButton").grid(row=2, column=2, sticky="ew", pady=6)

        ttk.Label(frame, text="FPS skanowania", style="App.TLabel").grid(row=3, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.scan_fps_var, width=12, style="App.TEntry").grid(row=3, column=1, sticky="w", padx=10, pady=6)

        actions = ttk.Frame(frame, style="App.TFrame")
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(24, 0))
        actions.columnconfigure((0, 1), weight=1)
        ttk.Button(actions, text="Anuluj", command=self.close_scan_dialog, style="App.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(actions, text="Skanuj choreografie", command=self.start_scan, style="Primary.TButton").grid(row=0, column=1, sticky="ew", padx=(8, 0))

    def close_scan_dialog(self) -> None:
        if self.scan_dialog is not None and self.scan_dialog.winfo_exists():
            self.scan_dialog.grab_release()
            self.scan_dialog.destroy()
        self.scan_dialog = None

    def pick_video(self) -> None:
        path = filedialog.askopenfilename(
            title="Wybierz film do skanowania",
            filetypes=[("Video files", "*.mp4 *.mov *.avi *.mkv"), ("All files", "*.*")],
        )
        if path:
            self.video_path_var.set(path)
            suggested = Path(path).with_suffix(".json")
            self.output_path_var.set(str(suggested))
            self.status_var.set("Wybrano film do skanowania.")

    def pick_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Wybierz plik JSON",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.output_path_var.set(path)

    def pick_choreography(self) -> None:
        path = filedialog.askopenfilename(
            title="Wybierz plik choreografii",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            json_path = Path(path).resolve()
            try:
                load_choreography(json_path)
            except Exception as exc:
                messagebox.showerror("Bledny plik", f"Nie udalo sie wczytac choreografii JSON:\n{exc}")
                return
            self._remember_choreography_path(json_path)
            self.choreography_path_var.set(str(json_path))
            self.status_var.set("Wybrano gotowa choreografie.")
            self.refresh_choreography_library()
            self.select_choreography_path(json_path)

    def add_choreography_to_library(self) -> None:
        path = filedialog.askopenfilename(
            title="Dodaj choreografie do listy",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return

        json_path = Path(path).resolve()
        try:
            load_choreography(json_path)
        except Exception as exc:
            messagebox.showerror("Bledny plik", f"Nie udalo sie wczytac choreografii JSON:\n{exc}")
            return

        self._remember_choreography_path(json_path)
        self.choreography_path_var.set(str(json_path))
        self.refresh_choreography_library()
        self.select_choreography_path(json_path)
        self.status_var.set("Dodano choreografie do listy.")

    def refresh_choreography_library(self) -> None:
        self._stop_menu_preview()
        for item_id in self.choreography_tree.get_children():
            self.choreography_tree.delete(item_id)
        self.choreography_items.clear()

        selected_path = Path(self.choreography_path_var.get().strip())
        json_paths: list[Path] = []
        for json_path in sorted(Path.cwd().rglob("*.json")):
            relative_path = json_path.relative_to(Path.cwd())
            if any(part.startswith(".") for part in relative_path.parts):
                continue
            if json_path.resolve() == self.library_path:
                continue
            json_paths.append(json_path)
        json_paths.extend(self._load_remembered_choreography_paths())

        seen_paths: set[str] = set()
        for json_path in json_paths:
            resolved_json_path = json_path.resolve()
            item_id = str(resolved_json_path)
            if item_id in seen_paths:
                continue
            seen_paths.add(item_id)
            if not resolved_json_path.exists():
                continue
            try:
                data = load_choreography(resolved_json_path)
            except Exception:
                continue

            source_video_path = Path(data.source_video)
            if not source_video_path.is_absolute():
                source_video_path = (resolved_json_path.parent / source_video_path).resolve()
            title = source_video_path.stem if source_video_path.name else resolved_json_path.stem
            duration = self._format_duration(data.duration_seconds)
            self.choreography_items[item_id] = {
                "json": item_id,
                "source_video": str(source_video_path),
            }
            self.choreography_tree.insert(
                "",
                "end",
                iid=item_id,
                values=(title, duration, str(data.frame_count)),
            )

        if self.choreography_items:
            self.select_choreography_path(selected_path)
            if not self.choreography_tree.selection():
                first_item = next(iter(self.choreography_items))
                self.choreography_tree.selection_set(first_item)
                self.choreography_tree.focus(first_item)
                self.on_choreography_selected()
        else:
            self.menu_preview_label.config(text="Brak choreografii", image="")
            self.status_var.set("Brak choreografii w folderze projektu. Zeskanuj film albo wybierz JSON recznie.")

    def _load_remembered_choreography_paths(self) -> list[Path]:
        if not self.library_path.exists():
            return []
        try:
            payload = json.loads(self.library_path.read_text(encoding="utf-8"))
        except Exception:
            return []

        raw_paths = payload.get("choreographies", []) if isinstance(payload, dict) else payload
        paths: list[Path] = []
        if not isinstance(raw_paths, list):
            return paths
        for item in raw_paths:
            if isinstance(item, dict):
                path_value = item.get("path")
            else:
                path_value = item
            if isinstance(path_value, str) and path_value.strip():
                paths.append(Path(path_value))
        return paths

    def _save_remembered_choreography_paths(self, paths: list[Path]) -> None:
        seen_paths: set[str] = set()
        entries = []
        for path in paths:
            resolved_path = str(path.resolve())
            if resolved_path in seen_paths:
                continue
            seen_paths.add(resolved_path)
            entries.append({"path": resolved_path})

        payload = {"choreographies": entries}
        self.library_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _remember_choreography_path(self, path: Path) -> None:
        remembered_paths = self._load_remembered_choreography_paths()
        remembered_paths.append(path.resolve())
        self._save_remembered_choreography_paths(remembered_paths)

    def _load_leaderboard_entries(self) -> list[dict[str, object]]:
        if not self.leaderboard_path.exists():
            return []
        try:
            payload = json.loads(self.leaderboard_path.read_text(encoding="utf-8"))
        except Exception:
            return []

        entries = payload.get("scores", []) if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    def _save_leaderboard_entries(self, entries: list[dict[str, object]]) -> None:
        payload = {"scores": entries[:100]}
        self.leaderboard_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def refresh_leaderboard(self) -> None:
        if not hasattr(self, "leaderboard_tree"):
            return
        for item_id in self.leaderboard_tree.get_children():
            self.leaderboard_tree.delete(item_id)

        entries = self._load_leaderboard_entries()
        entries.sort(key=lambda entry: float(entry.get("score", 0.0)), reverse=True)
        for index, entry in enumerate(entries[:20]):
            score = float(entry.get("score", 0.0))
            self.leaderboard_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    str(entry.get("player", "")),
                    str(entry.get("song", "")),
                    f"{score:.1f}",
                    str(entry.get("grade") or final_grade(score)),
                    str(entry.get("difficulty", "")),
                    str(entry.get("date", "")),
                ),
            )

    def save_score_to_leaderboard(self, choreography_path: Path, score: float, difficulty: str) -> None:
        player_name = self.player_name_var.get().strip() or "Gracz"
        self.remember_account(player_name)
        song_name = choreography_path.stem
        try:
            data = load_choreography(choreography_path)
            source_video_path = Path(data.source_video)
            song_name = source_video_path.stem if source_video_path.name else choreography_path.stem
        except Exception:
            pass

        entries = self._load_leaderboard_entries()
        entries.append(
            {
                "player": player_name,
                "song": song_name,
                "score": round(score, 2),
                "grade": final_grade(score),
                "difficulty": difficulty,
                "date": time.strftime("%Y-%m-%d %H:%M"),
                "choreography": str(choreography_path.resolve()),
            }
        )
        entries.sort(key=lambda entry: float(entry.get("score", 0.0)), reverse=True)
        self._save_leaderboard_entries(entries)
        self.refresh_leaderboard()

    def select_choreography_path(self, path: Path) -> None:
        item_id = str(path.resolve())
        if item_id in self.choreography_items:
            self.choreography_tree.selection_set(item_id)
            self.choreography_tree.focus(item_id)
            self.choreography_tree.see(item_id)
            self.on_choreography_selected()

    def on_choreography_selected(self, _event=None) -> None:
        selection = self.choreography_tree.selection()
        if not selection:
            return

        item = self.choreography_items.get(selection[0])
        if not item:
            return

        self.choreography_path_var.set(item["json"])
        source_video_path = Path(item["source_video"])
        self.status_var.set(f"Wybrano choreografie: {Path(item['json']).name}")
        if self.app_started:
            self._start_menu_preview(source_video_path)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total_seconds = max(0, int(round(seconds)))
        minutes = total_seconds // 60
        rest = total_seconds % 60
        return f"{minutes}:{rest:02d}"

    def _start_menu_preview(self, video_path: Path) -> None:
        self._stop_menu_preview()
        self.menu_preview_video_path = video_path
        if not video_path.exists():
            self.menu_preview_label.config(text="Brak filmu", image="")
            return

        self.menu_preview_cap = cv2.VideoCapture(str(video_path))
        if not self.menu_preview_cap.isOpened():
            self.menu_preview_cap.release()
            self.menu_preview_cap = None
            self.menu_preview_label.config(text="Brak podgladu", image="")
            return

        video_fps = self.menu_preview_cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.menu_preview_delay_ms = max(16, int(round(1000.0 / max(video_fps, 1.0))))
        self._start_menu_audio(video_path)
        self._update_menu_preview_frame()

    def _start_menu_audio(self, video_path: Path) -> None:
        self._stop_menu_audio()

        self.menu_audio_player = AudioPlayer(video_path, prefer_python_vlc=True)
        self.menu_audio_player.set_muted(self.menu_music_muted_var.get())
        if not self.menu_audio_player.play():
            print(f"Audio podgladu nie wystartowalo: {self.menu_audio_player.status_text()}")

    def _stop_menu_audio(self) -> None:
        if self.menu_audio_player is not None:
            self.menu_audio_player.stop()
            self.menu_audio_player = None

    def toggle_menu_music(self) -> None:
        muted = not self.menu_music_muted_var.get()
        self.menu_music_muted_var.set(muted)
        self.menu_music_button.config(text="Wlacz muzyke" if muted else "Wycisz muzyke")

        if self.menu_audio_player is not None:
            self.menu_audio_player.set_muted(muted)
        elif not muted and self.menu_preview_video_path is not None and self.menu_preview_video_path.exists():
            self._start_menu_audio(self.menu_preview_video_path)

    def _update_menu_preview_frame(self) -> None:
        if self.menu_preview_cap is None:
            return

        frame_started_at = time.perf_counter()
        ok, frame = self.menu_preview_cap.read()
        if not ok:
            self.menu_preview_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            if self.menu_preview_video_path is not None:
                self._start_menu_audio(self.menu_preview_video_path)
            ok, frame = self.menu_preview_cap.read()
            if not ok:
                self._stop_menu_preview()
                self.menu_preview_label.config(text="Brak podgladu", image="")
                return

        frame = downscale_frame_for_performance(frame, BACKGROUND_MAX_FRAME_WIDTH)
        try:
            self.menu_preview_image = self._frame_to_photo_image(frame, 260, 150)
        except tk.TclError:
            self.menu_preview_image = None
        if self.menu_preview_image is not None:
            self.menu_preview_label.config(image=self.menu_preview_image, text="")
        else:
            self.menu_preview_label.config(text="Brak podgladu", image="")
        processing_ms = int((time.perf_counter() - frame_started_at) * 1000)
        next_delay_ms = max(1, self.menu_preview_delay_ms - processing_ms)
        self.menu_preview_after_id = self.root.after(next_delay_ms, self._update_menu_preview_frame)

    @staticmethod
    def _frame_to_photo_image(frame, width: int, height: int):
        frame_height, frame_width = frame.shape[:2]
        scale = min(width / frame_width, height / frame_height)
        resized_width = max(1, int(frame_width * scale))
        resized_height = max(1, int(frame_height * scale))
        resized = cv2.resize(frame, (resized_width, resized_height), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        ok, encoded = cv2.imencode(".png", rgb)
        if not ok:
            return None
        data = base64.b64encode(encoded.tobytes()).decode("ascii")
        return tk.PhotoImage(data=data, format="PNG")

    def _stop_menu_preview(self) -> None:
        self._stop_menu_audio()
        if self.menu_preview_after_id is not None:
            self.root.after_cancel(self.menu_preview_after_id)
            self.menu_preview_after_id = None
        if self.menu_preview_cap is not None:
            self.menu_preview_cap.release()
            self.menu_preview_cap = None
        self.menu_preview_video_path = None

    def close(self) -> None:
        self._stop_menu_preview()
        self.root.destroy()

    def start_camera_preview(self) -> None:
        if self.busy:
            return

        try:
            camera_index = self.get_selected_camera_index()
        except ValueError:
            messagebox.showerror("Bledne dane", "Wybierz poprawna kamerke.")
            return

        self._stop_menu_preview()
        self._set_busy(True, "Podglad kamerki uruchomiony. Zamknij go klawiszem Q.")
        config = AppConfig(camera_index=camera_index, mirror_view=self.mirror_view_var.get())
        thread = threading.Thread(
            target=self._run_camera_preview,
            args=(config,),
            daemon=True,
        )
        thread.start()

    def start_scan(self) -> None:
        if self.busy:
            return
        video_path = Path(self.video_path_var.get().strip())
        output_path = Path(self.output_path_var.get().strip())
        if not video_path.exists():
            messagebox.showerror("Brak filmu", "Wybierz poprawny plik wideo do skanowania.")
            return

        try:
            scan_fps = float(self.scan_fps_var.get().strip())
            camera_index = self.get_selected_camera_index()
        except ValueError:
            messagebox.showerror("Bledne dane", "FPS skanowania musi byc liczba, a kamerka musi byc wybrana z listy.")
            return

        self._stop_menu_preview()
        self._set_busy(True, "Trwa skanowanie filmu. Okno podgladu otworzy sie osobno.")
        config = AppConfig(camera_index=camera_index, mirror_view=self.mirror_view_var.get())
        thread = threading.Thread(
            target=self._run_scan,
            args=(video_path, output_path, config, scan_fps),
            daemon=True,
        )
        thread.start()

    def start_play(self) -> None:
        if self.busy:
            return
        choreography_path = Path(self.choreography_path_var.get().strip())
        if not choreography_path.exists():
            messagebox.showerror("Brak pliku", "Wybierz poprawny plik choreografii JSON.")
            return

        try:
            camera_index = self.get_selected_camera_index()
        except ValueError:
            messagebox.showerror("Bledne dane", "Wybierz poprawna kamerke.")
            return

        difficulty = self.difficulty_var.get().strip()
        if difficulty not in DIFFICULTY_CONFIGS:
            messagebox.showerror("Bledne dane", "Wybierz poprawny poziom trudnosci.")
            return

        if not self.player_name_var.get().strip():
            messagebox.showerror("Brak nazwy", "Wpisz nazwe gracza przed startem.")
            return

        self._stop_menu_preview()
        self._set_busy(True, "Uruchamianie gry z kamerka. Okno podgladu otworzy sie osobno.")
        config = AppConfig(
            camera_index=camera_index,
            mirror_view=self.mirror_view_var.get(),
            difficulty=difficulty,
        )
        thread = threading.Thread(
            target=self._run_play,
            args=(choreography_path, config),
            daemon=True,
        )
        thread.start()

    def start_edit_choreography(self) -> None:
        if self.busy:
            return
        choreography_path = Path(self.choreography_path_var.get().strip())
        if not choreography_path.exists():
            messagebox.showerror("Brak pliku", "Wybierz poprawny plik choreografii JSON.")
            return

        self._stop_menu_preview()
        self._set_busy(True, "Edytor szkieletu otwarty. Zapisz klawiszem S, zamknij Q lub Esc.")
        thread = threading.Thread(
            target=self._run_edit_choreography,
            args=(choreography_path,),
            daemon=True,
        )
        thread.start()

    def _run_scan(self, video_path: Path, output_path: Path, config: AppConfig, scan_fps: float) -> None:
        try:
            scan_video(video_path, output_path, config, scan_fps)
            self.root.after(0, self.close_scan_dialog)
            self.root.after(0, lambda: self._finish_success(f"Skan zakonczony. Zapisano: {output_path}"))
            self.root.after(0, lambda: self.choreography_path_var.set(str(output_path)))
            self.root.after(0, lambda: self._remember_choreography_path(output_path))
            self.root.after(0, self.refresh_choreography_library)
            self.root.after(0, lambda: self.select_choreography_path(output_path))
        except SystemExit as exc:
            code = exc.code
            self.root.after(0, lambda: self._finish_error(f"Skanowanie przerwane. Kod: {code}"))
        except Exception as exc:
            message = str(exc)
            self.root.after(0, lambda: self._finish_error(f"Blad skanowania: {message}"))

    def _run_camera_preview(self, config: AppConfig) -> None:
        try:
            preview_camera(config)
            self.root.after(0, lambda: self._finish_success("Podglad kamerki zamkniety. Mozesz zaczac gre."))
        except SystemExit as exc:
            code = exc.code
            self.root.after(0, lambda: self._finish_error(f"Brak takiej kamerki, wybierz inną (zazwyczaj kolejny numer). Kod: {code}"))
        except Exception as exc:
            message = str(exc)
            self.root.after(0, lambda: self._finish_error(f"Blad podgladu kamerki: {message}"))

    def _run_play(self, choreography_path: Path, config: AppConfig) -> None:
        try:
            final_score = play_choreography(choreography_path, config)
            if final_score is None:
                self.root.after(0, lambda: self._finish_success("Sesja zakonczona bez zapisu wyniku."))
                return
            self.root.after(0, lambda: self.save_score_to_leaderboard(choreography_path, final_score, config.difficulty))
            self.root.after(
                0,
                lambda: self._finish_success(
                    f"Sesja zakonczona. Wynik zapisany: {final_score:.1f}/100 ({final_grade(final_score)})."
                ),
            )
        except SystemExit as exc:
            code = exc.code
            self.root.after(0, lambda: self._finish_error(f"Tryb gry przerwany. Kod: {code}"))
        except Exception as exc:
            message = str(exc)
            self.root.after(0, lambda: self._finish_error(f"Blad podczas gry: {message}"))

    def _run_edit_choreography(self, choreography_path: Path) -> None:
        try:
            edit_choreography(choreography_path)
            self.root.after(0, self.refresh_choreography_library)
            self.root.after(0, lambda: self.select_choreography_path(choreography_path))
            self.root.after(0, lambda: self._finish_success("Edycja choreografii zakonczona."))
        except Exception as exc:
            message = str(exc)
            self.root.after(0, lambda: self._finish_error(f"Blad edytora choreografii: {message}"))

    def _set_busy(self, busy: bool, status: str) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.scan_button.config(state=state)
        self.preview_button.config(state=state)
        self.play_button.config(state=state)
        if hasattr(self, "edit_button"):
            self.edit_button.config(state=state)
        self.status_var.set(status)

    def _finish_success(self, status: str) -> None:
        self._set_busy(False, status)

    def _finish_error(self, status: str) -> None:
        self._set_busy(False, status)
        messagebox.showerror("Blad", status)

    def run(self) -> None:
        self.root.mainloop()


if QObject is not None:
    class QtTaskSignals(QObject):
        finished = Signal(str, bool, str, object)


    class PreviewHoverFilter(QObject):
        interacted = Signal()

        def eventFilter(self, watched, event) -> bool:
            if event.type() in (QEvent.Enter, QEvent.MouseMove):
                self.interacted.emit()
            return False


    class PreviewIconButton(QPushButton):
        def __init__(self, icon_kind: str) -> None:
            super().__init__("")
            self.icon_kind = icon_kind
            self.setCursor(Qt.PointingHandCursor)
            self.setFixedSize(42, 38)
            self.setStyleSheet("background:transparent;border:0;")

        def set_icon_kind(self, icon_kind: str) -> None:
            self.icon_kind = icon_kind
            self.update()

        def paintEvent(self, _event) -> None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing, True)
            hover = self.underMouse()

            icon_color = QColor(255, 255, 255, 255 if not hover else 230)
            painter.setPen(QPen(icon_color, 3.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(icon_color)

            cx = self.width() / 2
            cy = self.height() / 2
            if self.icon_kind == "play":
                points = QPolygonF(
                    [
                        QPointF(cx - 6, cy - 9),
                        QPointF(cx - 6, cy + 9),
                        QPointF(cx + 9, cy),
                    ]
                )
                painter.drawPolygon(points)
            elif self.icon_kind == "pause":
                painter.setPen(Qt.NoPen)
                painter.drawRoundedRect(QRectF(cx - 9, cy - 10, 5, 20), 1.5, 1.5)
                painter.drawRoundedRect(QRectF(cx + 4, cy - 10, 5, 20), 1.5, 1.5)
            elif self.icon_kind == "fullscreen":
                left = cx - 12
                right = cx + 12
                top = cy - 10
                bottom = cy + 10
                corner = 7
                painter.drawLine(QPointF(left, top + corner), QPointF(left, top))
                painter.drawLine(QPointF(left, top), QPointF(left + corner, top))
                painter.drawLine(QPointF(right - corner, top), QPointF(right, top))
                painter.drawLine(QPointF(right, top), QPointF(right, top + corner))
                painter.drawLine(QPointF(left, bottom - corner), QPointF(left, bottom))
                painter.drawLine(QPointF(left, bottom), QPointF(left + corner, bottom))
                painter.drawLine(QPointF(right - corner, bottom), QPointF(right, bottom))
                painter.drawLine(QPointF(right, bottom), QPointF(right, bottom - corner))


def account_level(points: int) -> int:
    return max(1, int(points // 500) + 1)


if QApplication is not None:
    class IntroAnimation(QWidget):
        finished = Signal()

        def __init__(self) -> None:
            super().__init__()
            self.started_at = 0.0
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.update_frame)

        def start(self) -> None:
            self.started_at = time.perf_counter()
            self.timer.start(16)
            self.update()

        def update_frame(self) -> None:
            elapsed = time.perf_counter() - self.started_at
            if elapsed >= 3.15:
                self.timer.stop()
                self.finished.emit()
                return
            self.update()

        @staticmethod
        def ease_out_cubic(value: float) -> float:
            value = max(0.0, min(1.0, value))
            return 1 - ((1 - value) ** 3)

        @staticmethod
        def ease_in_cubic(value: float) -> float:
            value = max(0.0, min(1.0, value))
            return value ** 3

        def animated_x(self, start_x: float, target_x: float, start_time: float, duration: float, elapsed: float) -> float:
            progress = self.ease_out_cubic((elapsed - start_time) / duration)
            return start_x + (target_x - start_x) * progress

        def paintEvent(self, _event) -> None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.fillRect(self.rect(), QColor("#000000"))

            elapsed = time.perf_counter() - self.started_at if self.started_at else 0.0
            width = max(1, self.width())
            height = max(1, self.height())

            dance_font = QFont("Segoe UI", max(54, min(118, width // 10)), QFont.Black)
            dance_metrics = QFontMetrics(dance_font)
            dance_width = dance_metrics.horizontalAdvance("DANCE")
            gap = max(6, width // 180)
            target_left = (width - dance_width) / 2

            with_font = QFont("Segoe UI", max(24, int(dance_font.pointSize() * 0.46)), QFont.Bold)
            me_font = QFont("Segoe UI", max(40, int(dance_font.pointSize() * 0.78)), QFont.Black)
            for _ in range(80):
                with_width = QFontMetrics(with_font).horizontalAdvance("with")
                me_width = QFontMetrics(me_font).horizontalAdvance("ME")
                combined_width = with_width + gap + me_width
                if combined_width >= dance_width * 0.96:
                    break
                with_font.setPointSize(with_font.pointSize() + 1)
                me_font.setPointSize(me_font.pointSize() + 1)
            with_metrics = QFontMetrics(with_font)
            me_metrics = QFontMetrics(me_font)
            with_width = with_metrics.horizontalAdvance("with")
            me_width = me_metrics.horizontalAdvance("ME")
            me_x_target = target_left + with_width + gap

            dance_baseline = height * 0.50
            second_baseline = dance_baseline + max(82, dance_metrics.height() * 0.78)
            dance_x = self.animated_x(-dance_width - 80, target_left, 0.05, 0.82, elapsed)
            with_x = self.animated_x(width + 80, target_left, 0.42, 0.78, elapsed)
            me_x = self.animated_x(-me_width - 80, me_x_target, 0.78, 0.74, elapsed)

            exit_progress = self.ease_in_cubic((elapsed - 2.32) / 0.72)
            scale = 1.0 + exit_progress * 1.35
            alpha = int(255 * (1.0 - max(0.0, (elapsed - 2.58) / 0.44)))
            alpha = max(0, min(255, alpha))

            painter.translate(width / 2, height / 2)
            painter.scale(scale, scale)
            painter.translate(-width / 2, -height / 2)
            painter.translate(0, -exit_progress * height * 0.18)

            blur_offsets = [0]
            if exit_progress > 0:
                spread = int(2 + exit_progress * 18)
                blur_offsets = [-spread, -spread // 2, 0, spread // 2, spread]

            def draw_word(
                text: str,
                font: QFont,
                x: float,
                y: float,
                base_alpha: int,
            ) -> None:
                painter.setFont(font)
                painter.save()
                painter.translate(float(x), float(y))
                for offset in blur_offsets:
                    blur_alpha = int(base_alpha * (0.2 if offset else 1.0))
                    painter.setPen(QColor(237, 245, 247, max(0, min(255, blur_alpha))))
                    painter.drawText(int(offset), 0, text)
                painter.restore()

            draw_word("DANCE", dance_font, dance_x, dance_baseline, alpha)
            draw_word("with", with_font, with_x, second_baseline, alpha)
            draw_word("ME", me_font, me_x, second_baseline, alpha)


    class VideoBackground(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.video_path: Path | None = None
            self.cap = None
            self.frame = None
            self.frame_pixmap: QPixmap | None = None
            self.media_player = None
            self.audio_output = None
            self.video_sink = None
            if QMediaPlayer is not None and QAudioOutput is not None and QVideoSink is not None:
                self.media_player = QMediaPlayer(self)
                self.audio_output = QAudioOutput(self)
                self.video_sink = QVideoSink(self)
                self.media_player.setAudioOutput(self.audio_output)
                self.media_player.setVideoSink(self.video_sink)
                self.video_sink.videoFrameChanged.connect(self.on_media_frame_changed)
                self.media_player.mediaStatusChanged.connect(self.on_media_status_changed)
                if hasattr(self.media_player, "setLoops") and hasattr(QMediaPlayer, "Infinite"):
                    self.media_player.setLoops(QMediaPlayer.Infinite)
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.read_next_frame)
            self.fps = 30.0
            self.frame_count = 0
            self.duration_seconds = 0.0
            self.started_at = 0.0

        def set_video_path(self, video_path: Path | None) -> None:
            self.stop_video()
            self.video_path = video_path
            if video_path is None or not video_path.exists():
                self.update()
                return
            if self.has_combined_media():
                self.media_player.setSource(QUrl.fromLocalFile(str(video_path)))
                self.update()
                return
            self.cap = cv2.VideoCapture(str(video_path))
            if not self.cap.isOpened():
                self.cap.release()
                self.cap = None
                self.update()
                return
            self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
            self.frame_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            self.duration_seconds = (
                self.frame_count / self.fps
                if self.frame_count and self.fps
                else 0.0
            )
            self.started_at = time.perf_counter()
            self.timer.start(max(16, int(round(1000.0 / max(self.fps, 1.0)))))
            self.read_next_frame()

        def has_combined_media(self) -> bool:
            return self.media_player is not None and self.audio_output is not None

        def play_video(self, muted: bool = False) -> None:
            if self.video_path is None or not self.video_path.exists():
                return
            if self.has_combined_media():
                self.audio_output.setMuted(muted)
                self.media_player.setPosition(0)
                self.media_player.play()
                return
            self.restart_from_beginning()

        def set_muted(self, muted: bool) -> None:
            if self.has_combined_media():
                self.audio_output.setMuted(muted)

        def on_media_frame_changed(self, frame) -> None:
            image = frame.toImage()
            if image.isNull():
                return
            self.frame_pixmap = QPixmap.fromImage(image)
            self.update()

        def on_media_status_changed(self, status) -> None:
            if self.media_player is None or self.video_path is None:
                return
            if getattr(status, "name", "") == "EndOfMedia":
                self.media_player.setPosition(0)
                self.media_player.play()

        def read_next_frame(self) -> None:
            if self.cap is None:
                return
            ok, frame = self.cap.read()
            if not ok:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                self.started_at = time.perf_counter()
                ok, frame = self.cap.read()
            if ok:
                self.frame = downscale_frame_for_performance(frame, BACKGROUND_MAX_FRAME_WIDTH)
            self.update()

        def stop_video(self) -> None:
            self.timer.stop()
            if self.media_player is not None:
                self.media_player.stop()
            if self.cap is not None:
                self.cap.release()
                self.cap = None
            self.frame = None
            self.frame_pixmap = None
            self.frame_count = 0
            self.duration_seconds = 0.0
            self.started_at = 0.0

        def current_time_seconds(self) -> float:
            if self.has_combined_media() and self.media_player is not None:
                return max(0.0, self.media_player.position() / 1000.0)
            if self.cap is None or self.duration_seconds <= 0:
                return 0.0
            return (time.perf_counter() - self.started_at) % self.duration_seconds

        def restart_from_beginning(self) -> None:
            if self.has_combined_media() and self.media_player is not None:
                self.media_player.setPosition(0)
                self.media_player.play()
                return
            if self.cap is None:
                return
            self.started_at = time.perf_counter()
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            self.read_next_frame()

        def paintEvent(self, _event) -> None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            pixmap = self.frame_pixmap
            if pixmap is None and self.frame is not None:
                rgb = cv2.cvtColor(self.frame, cv2.COLOR_BGR2RGB)
                height, width, channels = rgb.shape
                image = QImage(rgb.data, width, height, channels * width, QImage.Format_RGB888).copy()
                pixmap = QPixmap.fromImage(image)
            if pixmap is not None and not pixmap.isNull():
                scaled_pixmap = pixmap.scaled(
                    self.size(),
                    Qt.KeepAspectRatioByExpanding,
                    Qt.SmoothTransformation,
                )
                x = (self.width() - scaled_pixmap.width()) // 2
                y = (self.height() - scaled_pixmap.height()) // 2
                painter.drawPixmap(x, y, scaled_pixmap)
                painter.fillRect(self.rect(), QColor(5, 10, 15, 170))
                return

            gradient = QLinearGradient(0, 0, self.width(), self.height())
            gradient.setColorAt(0.0, QColor("#071018"))
            gradient.setColorAt(0.55, QColor("#0c1821"))
            gradient.setColorAt(1.0, QColor("#10151f"))
            painter.fillRect(self.rect(), gradient)


    class LoginLogo(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setMinimumHeight(190)

        def paintEvent(self, _event) -> None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            width = max(1, self.width())

            dance_font = QFont("Segoe UI", max(46, min(92, width // 12)), QFont.Black)
            dance_metrics = QFontMetrics(dance_font)
            dance_width = dance_metrics.horizontalAdvance("DANCE")
            gap = max(6, width // 180)
            left = (width - dance_width) / 2

            with_font = QFont("Segoe UI", max(22, int(dance_font.pointSize() * 0.46)), QFont.Bold)
            me_font = QFont("Segoe UI", max(34, int(dance_font.pointSize() * 0.78)), QFont.Black)
            for _ in range(80):
                with_width = QFontMetrics(with_font).horizontalAdvance("with")
                me_width = QFontMetrics(me_font).horizontalAdvance("ME")
                if with_width + gap + me_width >= dance_width * 0.96:
                    break
                with_font.setPointSize(with_font.pointSize() + 1)
                me_font.setPointSize(me_font.pointSize() + 1)

            dance_baseline = 86
            second_baseline = dance_baseline + max(70, int(dance_metrics.height() * 0.72))

            def draw_text(text: str, font: QFont, x: float, y: float) -> None:
                painter.setFont(font)
                painter.setPen(QColor(0, 0, 0, 140))
                painter.drawText(int(x + 3), int(y + 4), text)
                painter.setPen(QColor("#edf5f7"))
                painter.drawText(int(x), int(y), text)

            with_width = QFontMetrics(with_font).horizontalAdvance("with")
            draw_text("DANCE", dance_font, left, dance_baseline)
            draw_text("with", with_font, left, second_baseline)
            draw_text("ME", me_font, left + with_width + gap, second_baseline)


    class AccountCarousel(QWidget):
        add_requested = Signal()
        activated = Signal()
        selected_changed = Signal(int)

        def __init__(self) -> None:
            super().__init__()
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.accounts: list[str] = []
            self.account_profiles: dict[str, dict[str, object]] = {}
            self.selected_index = 0
            self.animation_direction = 0
            self.animation_progress = 0.0
            self.animation_started_at = 0.0
            self.animation_duration = 0.36
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.update_animation)
            self.setMinimumHeight(360)

        def set_accounts(
            self,
            accounts: list[str],
            selected_index: int,
            account_profiles: dict[str, dict[str, object]] | None = None,
        ) -> None:
            self.accounts = accounts
            self.account_profiles = account_profiles or {}
            self.selected_index = selected_index if accounts else 0
            if self.accounts:
                self.selected_index %= len(self.accounts)
            self.animation_direction = 0
            self.animation_progress = 0.0
            self.timer.stop()
            self.update()

        def previous(self) -> None:
            self.start_slide(-1)

        def next(self) -> None:
            self.start_slide(1)

        def start_slide(self, direction: int) -> None:
            if len(self.accounts) <= 1 or self.timer.isActive():
                return
            self.animation_direction = direction
            self.animation_progress = 0.0
            self.animation_started_at = time.perf_counter()
            self.timer.start(16)

        def update_animation(self) -> None:
            elapsed = time.perf_counter() - self.animation_started_at
            progress = min(1.0, elapsed / self.animation_duration)
            self.animation_progress = 1 - ((1 - progress) ** 3)
            if progress >= 1.0:
                self.timer.stop()
                self.selected_index = (self.selected_index + self.animation_direction) % len(self.accounts)
                self.selected_changed.emit(self.selected_index)
                self.animation_direction = 0
                self.animation_progress = 0.0
            self.update()

        def paintEvent(self, _event) -> None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            center_x = self.width() / 2
            center_y = self.height() / 2

            if not self.accounts:
                self.draw_card(
                    painter,
                    center_x,
                    center_y,
                    290,
                    "＋\nDodaj konto",
                    QColor("#33c7a4"),
                    1.0,
                    1.0,
                )
                return

            draw_specs = []
            for relative_index in range(-3, 4):
                account_index = (self.selected_index + relative_index) % len(self.accounts)
                visual_relative = relative_index - (self.animation_direction * self.animation_progress)
                if abs(visual_relative) > 2.35:
                    continue
                depth = min(1.0, abs(visual_relative) / 2.0)
                scale = 1.0 - 0.34 * min(abs(visual_relative), 1.6)
                opacity = 1.0 - 0.55 * min(abs(visual_relative), 1.6)
                x = center_x + visual_relative * 250
                y = center_y + depth * 46
                size = 292 * scale
                draw_specs.append((abs(visual_relative), x, y, size, self.accounts[account_index], account_index, opacity, scale))

            for _depth, x, y, size, name, account_index, opacity, scale in sorted(draw_specs, reverse=True):
                self.draw_card(
                    painter,
                    x,
                    y,
                    size,
                    name,
                    self.account_profiles.get(name, {}),
                    self.account_color(account_index),
                    opacity,
                    scale,
                )

        def draw_card(
            self,
            painter: QPainter,
            center_x: float,
            center_y: float,
            size: float,
            text: str,
            profile: dict[str, object],
            color: QColor,
            opacity: float,
            scale: float,
        ) -> None:
            rect = QRectF(center_x - size / 2, center_y - size / 2, size, size)
            painter.save()
            painter.setOpacity(max(0.18, min(1.0, opacity)))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, int(95 * opacity)))
            painter.drawRoundedRect(rect.translated(0, 14), 28, 28)
            painter.setBrush(color)
            painter.drawRoundedRect(rect, 28, 28)
            avatar_size = size * 0.34
            avatar_rect = QRectF(center_x - avatar_size / 2, rect.top() + size * 0.15, avatar_size, avatar_size)
            self.draw_avatar(painter, avatar_rect, text, str(profile.get("avatar", "")))
            painter.setPen(QColor(0, 0, 0, int(90 * opacity)))
            font = QFont("Segoe UI", max(18, int(25 * scale)), QFont.Black)
            painter.setFont(font)
            name_rect = QRectF(rect.left() + 16, rect.top() + size * 0.52, rect.width() - 32, size * 0.23)
            painter.drawText(name_rect.translated(2, 3), Qt.AlignCenter | Qt.TextWordWrap, text)
            painter.setPen(QColor("#ffffff") if color.lightness() < 135 else QColor("#071018"))
            painter.drawText(name_rect, Qt.AlignCenter | Qt.TextWordWrap, text)
            xp = int(profile.get("xp", 0) or 0)
            level = account_level(xp)
            info = f"LVL {level}  |  {xp} pkt"
            info_font = QFont("Segoe UI", max(10, int(13 * scale)), QFont.Bold)
            painter.setFont(info_font)
            info_rect = QRectF(rect.left() + 12, rect.bottom() - size * 0.18, rect.width() - 24, size * 0.12)
            painter.setPen(QColor("#ffffff") if color.lightness() < 135 else QColor("#06241d"))
            painter.drawText(info_rect, Qt.AlignCenter, info)
            painter.restore()

        def draw_avatar(self, painter: QPainter, rect: QRectF, name: str, avatar_path: str) -> None:
            painter.save()
            path = QPainterPath()
            path.addEllipse(rect)
            painter.setClipPath(path)
            pixmap = QPixmap(avatar_path) if avatar_path and Path(avatar_path).exists() else QPixmap()
            if not pixmap.isNull():
                painter.drawPixmap(rect.toRect(), pixmap)
            else:
                painter.setBrush(QColor(255, 255, 255, 72))
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(rect)
                painter.setClipping(False)
                painter.setPen(QColor("#ffffff"))
                painter.setFont(QFont("Segoe UI", max(18, int(rect.height() * 0.42)), QFont.Black))
                painter.drawText(rect, Qt.AlignCenter, name[:1].upper() if name else "+")
            painter.restore()

        @staticmethod
        def account_color(index: int) -> QColor:
            palette = ["#33c7a4", "#6977ff", "#ff6b6b", "#f0b84f", "#8f6bff", "#49b8e8"]
            return QColor(palette[index % len(palette)])

        def mousePressEvent(self, event) -> None:
            if not self.accounts:
                self.add_requested.emit()
                return
            center_x = self.width() / 2
            if event.position().x() < center_x - 160:
                self.previous()
            elif event.position().x() > center_x + 160:
                self.next()
            else:
                self.activated.emit()


    class HandMenuController(QObject):
        def __init__(self, root_widget: QWidget, camera_index_provider=None) -> None:
            super().__init__(root_widget)
            self.root_widget = root_widget
            self.camera_index_provider = camera_index_provider
            self.cap = None
            self.hands = None
            self.pose = None
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.update_hand)
            self.cursor_x: float | None = None
            self.cursor_y: float | None = None
            self.was_fist = False
            self.fist_started_at: float | None = None
            self.last_click_at = 0.0
            self.cursor_overridden = False
            self.last_seen_hand_at = 0.0
            self.anchor_hand_x: float | None = None
            self.anchor_hand_y: float | None = None
            self.anchor_cursor_x: float | None = None
            self.anchor_cursor_y: float | None = None
            self.last_programmatic_cursor_pos: tuple[int, int] | None = None
            self.manual_mouse_until = 0.0
            self.suppress_centering = False
            self.armed = False
            self.activation_started_at: float | None = None
            self.control_ready_at = 0.0
            self.requires_reactivation = True

        def start(self) -> None:
            if self.timer.isActive():
                return
            camera_index = self.resolve_camera_index()
            self.cap = open_camera(camera_index, 640, 480)
            if self.cap is None:
                print(f"Sterowanie reka: nie udalo sie otworzyc kamerki {camera_index}.")
                return
            self.hands = mp.solutions.hands.Hands(
                static_image_mode=False,
                max_num_hands=1,
                model_complexity=0,
                min_detection_confidence=0.45,
                min_tracking_confidence=0.35,
            )
            self.pose = mp.solutions.pose.Pose(
                static_image_mode=False,
                model_complexity=0,
                enable_segmentation=False,
                smooth_landmarks=True,
                min_detection_confidence=0.45,
                min_tracking_confidence=0.35,
            )
            self.was_fist = False
            self.fist_started_at = None
            self.last_click_at = 0.0
            self.last_seen_hand_at = 0.0
            self.timer.start(33)

        def stop(self) -> None:
            self.timer.stop()
            self.restore_cursor()
            if self.cap is not None:
                self.cap.release()
                self.cap = None
            if self.hands is not None:
                self.hands.close()
                self.hands = None
            if self.pose is not None:
                self.pose.close()
                self.pose = None
            self.cursor_x = None
            self.cursor_y = None
            self.was_fist = False
            self.anchor_hand_x = None
            self.anchor_hand_y = None
            self.anchor_cursor_x = None
            self.anchor_cursor_y = None
            self.last_programmatic_cursor_pos = None
            self.manual_mouse_until = 0.0
            self.suppress_centering = False
            self.armed = False
            self.activation_started_at = None
            self.control_ready_at = 0.0
            self.requires_reactivation = True

        def resolve_camera_index(self) -> int:
            if self.camera_index_provider is not None:
                try:
                    return int(self.camera_index_provider())
                except Exception:
                    pass
            cameras = list_available_cameras()
            return cameras[0][1] if cameras else 0

        def update_hand(self) -> None:
            if self.cap is None or self.hands is None or self.pose is None:
                return
            now = time.perf_counter()
            self.update_manual_mouse_pause(now)
            if now < self.manual_mouse_until:
                self.was_fist = False
                self.restore_cursor(center=False)
                return

            ok, frame = self.cap.read()
            if not ok:
                return

            frame = cv2.flip(frame, 1)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = self.hands.process(rgb)
            pose_result = self.pose.process(rgb)
            if not result.multi_hand_landmarks:
                if self.last_seen_hand_at and time.perf_counter() - self.last_seen_hand_at > 0.4:
                    self.disarm()
                self.was_fist = False
                return

            self.last_seen_hand_at = time.perf_counter()
            hand_landmarks = self.pick_control_hand(result)
            if hand_landmarks is None:
                if self.last_seen_hand_at and time.perf_counter() - self.last_seen_hand_at > 0.4:
                    self.disarm()
                self.was_fist = False
                return

            if not self.armed:
                if self.is_wrist_near_shoulder(pose_result.pose_landmarks):
                    if self.activation_started_at is None:
                        self.activation_started_at = now
                        if not self.suppress_centering:
                            self.center_cursor()
                        self.set_hand_cursor(False)
                    if now - self.activation_started_at >= 1.0:
                        self.armed = True
                        self.control_ready_at = now
                        self.reset_anchor()
                    self.was_fist = False
                else:
                    self.activation_started_at = None
                    self.restore_cursor(center=False)
                    self.was_fist = False
                return

            cursor_landmark = hand_landmarks.landmark[9]
            screen = QApplication.primaryScreen()
            if screen is None:
                return
            geometry = screen.availableGeometry()
            hand_x = max(0.0, min(1.0, cursor_landmark.x))
            hand_y = max(0.0, min(1.0, cursor_landmark.y))
            if self.anchor_hand_x is None or self.anchor_hand_y is None:
                current_pos = QCursor.pos()
                self.anchor_hand_x = hand_x
                self.anchor_hand_y = hand_y
                self.anchor_cursor_x = float(current_pos.x())
                self.anchor_cursor_y = float(current_pos.y())

            sensitivity = 3.2
            target_x = float(self.anchor_cursor_x or geometry.center().x()) + ((hand_x - (self.anchor_hand_x or hand_x)) * geometry.width() * sensitivity)
            target_y = float(self.anchor_cursor_y or geometry.center().y()) + ((hand_y - (self.anchor_hand_y or hand_y)) * geometry.height() * sensitivity)
            target_x = max(float(geometry.left()), min(float(geometry.right()), target_x))
            target_y = max(float(geometry.top()), min(float(geometry.bottom()), target_y))
            if self.cursor_x is None or self.cursor_y is None:
                self.cursor_x = target_x
                self.cursor_y = target_y
            else:
                smoothing = 0.34
                self.cursor_x += (target_x - self.cursor_x) * smoothing
                self.cursor_y += (target_y - self.cursor_y) * smoothing

            raw_fist = self.is_fist(hand_landmarks)
            if raw_fist:
                if self.fist_started_at is None:
                    self.fist_started_at = now
                fist = now - self.fist_started_at >= 0.16
            else:
                self.fist_started_at = None
                fist = False
            self.set_hand_cursor(fist)
            self.move_cursor(int(self.cursor_x), int(self.cursor_y))
            self.suppress_centering = False
            if fist and not self.was_fist and now - self.last_click_at > 0.65:
                self.click_current_widget()
                self.last_click_at = now
            self.was_fist = fist

        def update_manual_mouse_pause(self, now: float) -> None:
            current_pos = QCursor.pos()
            current = (int(current_pos.x()), int(current_pos.y()))
            if self.last_programmatic_cursor_pos is None:
                self.last_programmatic_cursor_pos = current
                return
            distance = math.dist(current, self.last_programmatic_cursor_pos)
            if distance > 18.0 and now >= self.manual_mouse_until:
                self.manual_mouse_until = now + 3.0
                self.suppress_centering = True
                self.reset_anchor()
                self.last_programmatic_cursor_pos = current

        def move_cursor(self, x: int, y: int) -> None:
            QCursor.setPos(x, y)
            if sys.platform.startswith("win"):
                try:
                    ctypes.windll.user32.SetCursorPos(int(x), int(y))
                except Exception:
                    pass
            self.last_programmatic_cursor_pos = (int(x), int(y))

        def center_cursor(self) -> None:
            screen = QApplication.primaryScreen()
            if screen is None:
                return
            center = screen.availableGeometry().center()
            self.cursor_x = float(center.x())
            self.cursor_y = float(center.y())
            self.move_cursor(center.x(), center.y())
            self.reset_anchor()

        def pick_physical_right_hand(self, result):
            if not result.multi_hand_landmarks:
                return None
            if not result.multi_handedness:
                return result.multi_hand_landmarks[0]
            for index, handedness in enumerate(result.multi_handedness):
                classification = handedness.classification[0]
                if classification.label == "Right" and float(classification.score) >= 0.45:
                    return result.multi_hand_landmarks[index]
            return None

        def pick_control_hand(self, result):
            right_hand = self.pick_physical_right_hand(result)
            if right_hand is not None:
                return right_hand
            if result.multi_hand_landmarks and len(result.multi_hand_landmarks) == 1:
                return result.multi_hand_landmarks[0]
            return None

        @staticmethod
        def is_wrist_near_shoulder(pose_landmarks) -> bool:
            if pose_landmarks is None:
                return False
            landmarks = pose_landmarks.landmark
            pairs = (
                (POSE_LANDMARKS.RIGHT_WRIST, POSE_LANDMARKS.RIGHT_SHOULDER),
                (POSE_LANDMARKS.LEFT_WRIST, POSE_LANDMARKS.LEFT_SHOULDER),
            )
            for wrist_id, shoulder_id in pairs:
                wrist = landmarks[wrist_id.value]
                shoulder = landmarks[shoulder_id.value]
                if wrist.visibility < 0.35 or shoulder.visibility < 0.35:
                    continue
                if math.dist((wrist.x, wrist.y), (shoulder.x, shoulder.y)) <= 0.16:
                    return True
            return False

        def disarm(self) -> None:
            self.armed = False
            self.activation_started_at = None
            self.control_ready_at = 0.0
            self.requires_reactivation = True
            self.was_fist = False
            self.fist_started_at = None
            self.restore_cursor(center=False)

        def set_hand_cursor(self, fist: bool) -> None:
            shape = Qt.ClosedHandCursor if fist else Qt.OpenHandCursor
            if self.cursor_overridden:
                QApplication.changeOverrideCursor(QCursor(shape))
            else:
                QApplication.setOverrideCursor(QCursor(shape))
                self.cursor_overridden = True

        def reset_anchor(self) -> None:
            self.anchor_hand_x = None
            self.anchor_hand_y = None
            self.anchor_cursor_x = None
            self.anchor_cursor_y = None

        def restore_cursor(self, center: bool = False) -> None:
            if center and not self.suppress_centering:
                self.center_cursor()
            if self.cursor_overridden:
                QApplication.restoreOverrideCursor()
                self.cursor_overridden = False
            self.reset_anchor()

        @staticmethod
        def is_fist(hand_landmarks) -> bool:
            curled = 0
            for tip_id, pip_id in ((8, 6), (12, 10), (16, 14), (20, 18)):
                if hand_landmarks.landmark[tip_id].y > hand_landmarks.landmark[pip_id].y + 0.026:
                    curled += 1
            return curled >= 3

        def click_current_widget(self) -> None:
            global_pos = QCursor.pos()
            if sys.platform.startswith("win"):
                try:
                    ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                    ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                    return
                except Exception:
                    pass
            widget = QApplication.widgetAt(global_pos)
            if widget is None:
                return
            target = widget
            while target is not None:
                if isinstance(target, QPushButton) and target.isEnabled():
                    target.click()
                    return
                if target == self.root_widget:
                    break
                target = target.parentWidget()

            local_pos = widget.mapFromGlobal(global_pos)
            local_point = QPointF(local_pos)
            global_point = QPointF(global_pos)
            press = QMouseEvent(
                QEvent.MouseButtonPress,
                local_point,
                local_point,
                global_point,
                Qt.LeftButton,
                Qt.LeftButton,
                Qt.NoModifier,
            )
            release = QMouseEvent(
                QEvent.MouseButtonRelease,
                local_point,
                local_point,
                global_point,
                Qt.LeftButton,
                Qt.NoButton,
                Qt.NoModifier,
            )
            QApplication.sendEvent(widget, press)
            QApplication.sendEvent(widget, release)


class PySideDanceScorerGUI:
    def __init__(self) -> None:
        if QApplication is None:
            raise RuntimeError("Brakuje PySide6. Uruchom: python -m pip install -r requirements.txt")

        self.app = QApplication.instance() or QApplication(sys.argv)
        self.app.setApplicationName("Dance with me")
        self.window = QWidget()
        self.window.setWindowTitle("Dance with me")
        self.window.resize(1280, 800)
        self.window.setMinimumSize(1100, 720)
        self.window.showFullScreen()

        self.library_path = Path("choreography_library.json").resolve()
        self.leaderboard_path = Path("leaderboard.json").resolve()
        self.accounts_path = Path("player_accounts.json").resolve()
        self.hidden_choreographies_path = Path("hidden_choreographies.json").resolve()
        self.choreography_items: dict[str, dict[str, str]] = {}
        self.camera_choices: dict[str, int] = {}
        self.account_names: list[str] = []
        self.account_profiles: dict[str, dict[str, object]] = {}
        self.selected_account_index = 0
        self.current_player = ""
        self.busy = False
        self.scan_video_path = ""
        self.scan_output_path = str(Path("choreography.json").resolve())
        self.camera_width = 640
        self.camera_height = 480
        self.camera_fps = 30
        self.camera_prefer_mjpg = True
        self.pose_model_complexity = 0
        self.min_detection_confidence = 0.5
        self.min_tracking_confidence = 0.5
        self.game_fullscreen = True

        self.preview_cap = None
        self.preview_timer = QTimer(self.window)
        self.preview_timer.timeout.connect(self.update_menu_preview_frame)
        self.preview_video_path: Path | None = None
        self.preview_delay_ms = 33
        self.preview_started_at = 0.0
        self.preview_fps = 30.0
        self.preview_frame_count = 0
        self.preview_duration_seconds = 0.0
        self.preview_zoom_percent = 100
        self.preview_source_pixmap: QPixmap | None = None
        self.preview_timeline_duration_ms = 0
        self.preview_timeline_dragging = False
        self.preview_paused = False
        self.preview_fullscreen_dialog: QDialog | None = None
        self.preview_fullscreen_label: QLabel | None = None
        self.preview_fullscreen_controls_widget: QWidget | None = None
        self.preview_fullscreen_pause_button: QPushButton | None = None
        self.preview_fullscreen_time_slider: QSlider | None = None
        self.preview_fullscreen_time_label: QLabel | None = None
        self.preview_controls_hide_timer = QTimer(self.window)
        self.preview_controls_hide_timer.setSingleShot(True)
        self.preview_controls_hide_timer.timeout.connect(self.hide_preview_controls)
        self.preview_hover_filter = PreviewHoverFilter()
        self.preview_hover_filter.interacted.connect(self.show_preview_controls_temporarily)
        self.camera_preview_cap = None
        self.camera_preview_stream: LatestFrameCamera | None = None
        self.camera_preview_engine: PoseEngine | None = None
        self.camera_preview_timer = QTimer(self.window)
        self.camera_preview_timer.timeout.connect(self.update_inline_camera_preview)
        self.camera_preview_previous_time = 0.0
        self.camera_preview_active = False
        self.menu_audio_player: AudioPlayer | None = None
        self.menu_music_muted = False
        self.preview_media_player = None
        self.preview_audio_output = None
        self.preview_video_sink = None
        if QMediaPlayer is not None and QAudioOutput is not None and QVideoSink is not None:
            self.preview_media_player = QMediaPlayer(self.window)
            self.preview_audio_output = QAudioOutput(self.window)
            self.preview_video_sink = QVideoSink(self.window)
            self.preview_media_player.setAudioOutput(self.preview_audio_output)
            self.preview_media_player.setVideoSink(self.preview_video_sink)
            self.preview_video_sink.videoFrameChanged.connect(self.on_preview_media_frame_changed)
            self.preview_media_player.mediaStatusChanged.connect(self.on_preview_media_status_changed)
            self.preview_media_player.positionChanged.connect(self.on_preview_media_position_changed)
            self.preview_media_player.durationChanged.connect(self.on_preview_media_duration_changed)
            if hasattr(self.preview_media_player, "setLoops") and hasattr(QMediaPlayer, "Infinite"):
                self.preview_media_player.setLoops(QMediaPlayer.Infinite)
        self.login_audio_timer = QTimer(self.window)
        self.login_audio_timer.timeout.connect(self.sync_login_background_audio)
        self.hand_menu_controller: HandMenuController | None = None
        self.task_signals = QtTaskSignals()
        self.task_signals.finished.connect(self.on_task_finished)

        self.stack = QStackedWidget()
        root = QVBoxLayout(self.window)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.stack)

        self.apply_styles()
        self.build_intro_view()
        self.build_login_view()
        self.build_main_view()
        self.refresh_accounts()
        self.refresh_camera_choices()
        self.hand_menu_controller = HandMenuController(self.window, self.get_selected_camera_index)
        self.refresh_choreography_library()
        self.refresh_leaderboard()
        self.update_login_background()
        self.stack.setCurrentWidget(self.intro_page)

    def build_intro_view(self) -> None:
        self.intro_page = IntroAnimation()
        self.intro_page.finished.connect(self.show_login_after_intro)
        self.stack.addWidget(self.intro_page)

    def show_login_after_intro(self) -> None:
        self.stack.setCurrentWidget(self.login_page)
        self.start_login_background_audio()
        self.start_hand_menu_control()

    def apply_styles(self) -> None:
        self.app.setStyleSheet(
            """
            QWidget {
                background: #0b1117;
                color: #edf5f7;
                font-family: Segoe UI;
                font-size: 14px;
            }
            QLabel {
                background: transparent;
                border: 0;
            }
            QLabel#Title {
                font-size: 34px;
                font-weight: 800;
            }
            QLabel#Subtitle, QLabel#Status {
                color: #9fb2bf;
            }
            QFrame#Surface, QGroupBox {
                background: #121d26;
                border: 1px solid #253747;
                border-radius: 16px;
            }
            QGroupBox {
                margin-top: 18px;
                padding: 18px;
                font-weight: 700;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 14px;
                padding: 0 8px;
                background: transparent;
                color: #edf5f7;
            }
            QPushButton {
                background: #1c2b36;
                color: #edf5f7;
                border: 1px solid #314657;
                border-radius: 10px;
                padding: 10px 16px;
                font-weight: 700;
            }
            QPushButton:hover { background: #263a49; }
            QPushButton:disabled { color: #667782; background: #17232d; }
            QPushButton#Primary {
                background: #33c7a4;
                color: #06241d;
                border-color: #33c7a4;
            }
            QPushButton#Primary:hover { background: #43d9b5; }
            QPushButton#Danger {
                background: #42242a;
                color: #ffd7dd;
                border-color: #5d3038;
            }
            QPushButton#AccountArrow {
                background: transparent;
                border: 0;
                color: #edf5f7;
                font-size: 44px;
                padding: 4px 10px;
            }
            QPushButton#AccountArrow:hover {
                background: transparent;
                color: #33c7a4;
            }
            QLineEdit, QComboBox, QSpinBox {
                background: #0d141b;
                border: 1px solid #314657;
                border-radius: 9px;
                padding: 9px 10px;
                min-height: 22px;
            }
            QComboBox QAbstractItemView {
                background: #0d141b;
                color: #edf5f7;
                selection-background-color: #24443f;
                outline: 0;
            }
            QCheckBox {
                spacing: 10px;
                background: transparent;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
            }
            QListWidget, QTableWidget {
                background: #0d141b;
                border: 1px solid #253747;
                border-radius: 12px;
                outline: 0;
            }
            QListWidget::item {
                border-radius: 10px;
                padding: 12px;
                margin: 5px;
            }
            QListWidget::item:selected {
                background: #24443f;
                color: #edf5f7;
            }
            QListWidget::item:hover {
                background: #162631;
            }
            QHeaderView::section {
                background: #1c2b36;
                color: #edf5f7;
                border: 0;
                padding: 8px;
                font-weight: 700;
            }
            QTableWidget::item {
                padding: 8px;
                border: 0;
            }
            QTableWidget::item:selected {
                background: #24443f;
                color: #edf5f7;
            }
            QFrame#LoginSurface {
                background: rgba(18, 29, 38, 215);
                border: 1px solid rgba(51, 199, 164, 80);
                border-radius: 22px;
            }
            """
        )

    def build_login_view(self) -> None:
        self.login_page = VideoBackground()
        page = QGridLayout(self.login_page)
        page.setContentsMargins(34, 24, 34, 34)
        page.setHorizontalSpacing(18)
        page.setVerticalSpacing(16)
        page.setColumnStretch(0, 0)
        page.setColumnStretch(1, 1)
        page.setColumnStretch(2, 0)
        page.setRowStretch(0, 0)
        page.setRowStretch(1, 1)
        page.setRowStretch(2, 0)

        top = QHBoxLayout()
        top.setSpacing(14)
        add_button = QPushButton("Dodaj konto")
        add_button.clicked.connect(self.open_add_account_dialog)
        settings_button = QPushButton("Ustawienia konta")
        settings_button.clicked.connect(self.open_account_settings_dialog)
        delete_button = QPushButton("Usun konto")
        delete_button.setObjectName("Danger")
        delete_button.clicked.connect(self.delete_selected_account)
        exit_button = QPushButton("Wyjdz z gry")
        exit_button.setObjectName("Danger")
        exit_button.clicked.connect(self.close)
        top.addWidget(add_button)
        top.addWidget(settings_button)
        top.addStretch(1)
        top.addSpacing(24)
        top.addWidget(delete_button)
        top.addWidget(exit_button)
        page.addLayout(top, 0, 0, 1, 3)

        self.previous_account_button = QPushButton("<")
        self.previous_account_button.setObjectName("AccountArrow")
        self.previous_account_button.clicked.connect(self.previous_account)
        self.previous_account_button.setMinimumWidth(92)
        previous_shadow = QGraphicsDropShadowEffect(self.previous_account_button)
        previous_shadow.setBlurRadius(18)
        previous_shadow.setOffset(0, 4)
        previous_shadow.setColor(QColor(0, 0, 0, 180))
        self.previous_account_button.setGraphicsEffect(previous_shadow)
        self.next_account_button = QPushButton(">")
        self.next_account_button.setObjectName("AccountArrow")
        self.next_account_button.clicked.connect(self.next_account)
        self.next_account_button.setMinimumWidth(92)
        next_shadow = QGraphicsDropShadowEffect(self.next_account_button)
        next_shadow.setBlurRadius(18)
        next_shadow.setOffset(0, 4)
        next_shadow.setColor(QColor(0, 0, 0, 180))
        self.next_account_button.setGraphicsEffect(next_shadow)

        self.account_carousel = AccountCarousel()
        self.account_carousel.add_requested.connect(self.open_add_account_dialog)
        self.account_carousel.activated.connect(self.enter_app)
        self.account_carousel.selected_changed.connect(self.on_account_carousel_selected)

        center = QWidget()
        center.setStyleSheet("background: transparent;")
        center.setMinimumWidth(860)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(18)
        self.login_logo = LoginLogo()
        self.login_logo.setMinimumWidth(760)
        self.account_carousel.setMinimumWidth(860)
        center_layout.addWidget(self.login_logo, 0, Qt.AlignHCenter | Qt.AlignTop)
        center_layout.addWidget(self.account_carousel, 1, Qt.AlignCenter)

        page.addWidget(self.previous_account_button, 1, 0, alignment=Qt.AlignVCenter | Qt.AlignLeft)
        page.addWidget(center, 1, 1, alignment=Qt.AlignCenter)
        page.addWidget(self.next_account_button, 1, 2, alignment=Qt.AlignVCenter | Qt.AlignRight)

        play_button = QPushButton("Graj")
        play_button.setObjectName("Primary")
        play_button.clicked.connect(self.enter_app)
        play_button.setMaximumWidth(360)
        page.addWidget(play_button, 2, 1, alignment=Qt.AlignHCenter)
        self.stack.addWidget(self.login_page)
        return

    def build_main_view(self) -> None:
        self.main_page = QWidget()
        page = QVBoxLayout(self.main_page)
        page.setContentsMargins(34, 26, 34, 30)
        page.setSpacing(18)

        topbar = QHBoxLayout()
        title = QLabel("Dance with me")
        title.setObjectName("Title")
        settings_button = QPushButton("Ustawienia")
        settings_button.clicked.connect(self.open_account_settings_dialog)
        change_button = QPushButton("Zmien konto")
        change_button.clicked.connect(self.change_account)
        self.player_avatar_label = QLabel("")
        self.player_avatar_label.setFixedSize(42, 42)
        self.player_badge = QLabel("")
        self.player_badge.setObjectName("Subtitle")
        topbar.addWidget(title)
        topbar.addStretch(1)
        topbar.addWidget(settings_button)
        topbar.addWidget(change_button)
        topbar.addWidget(self.player_avatar_label)
        topbar.addWidget(self.player_badge)
        page.addLayout(topbar)

        controls = QFrame()
        controls.setObjectName("Surface")
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(18, 16, 18, 16)
        controls_layout.setSpacing(12)
        self.camera_combo = QComboBox()
        self.mirror_check = QCheckBox("Odbicie lustrzane")
        self.mirror_check.setChecked(True)
        self.camera_settings_button = QPushButton("Wiecej ustawien")
        self.camera_settings_button.clicked.connect(self.open_camera_settings_dialog)
        self.camera_preview_button = QPushButton("Podglad kamerki")
        self.camera_preview_button.clicked.connect(self.start_camera_preview)
        scan_button = QPushButton("Skanuj choreografie")
        scan_button.clicked.connect(self.open_scan_dialog)
        self.play_button = QPushButton("Graj")
        self.play_button.setObjectName("Primary")
        self.play_button.clicked.connect(self.start_play)
        self.busy_buttons = [self.camera_preview_button, self.camera_settings_button, scan_button, self.play_button]

        controls_layout.addWidget(QLabel("Kamerka"))
        controls_layout.addWidget(self.camera_combo, 2)
        controls_layout.addWidget(self.mirror_check)
        controls_layout.addWidget(self.camera_settings_button)
        controls_layout.addWidget(self.camera_preview_button)
        controls_layout.addWidget(scan_button)
        controls_layout.addWidget(self.play_button)
        page.addWidget(controls)

        content = QGridLayout()
        content.setColumnStretch(0, 3)
        content.setColumnStretch(1, 2)
        content.setSpacing(18)

        library_box = QGroupBox("Choreografie")
        library_layout = QVBoxLayout(library_box)
        self.choreography_list = QListWidget()
        self.choreography_list.currentItemChanged.connect(lambda _current, _previous: self.on_choreography_selected())
        library_layout.addWidget(self.choreography_list, 1)
        library_actions = QHBoxLayout()
        add_choreo = QPushButton("Dodaj choreografie")
        add_choreo.clicked.connect(self.add_choreography_to_library)
        remove_choreo = QPushButton("Usun choreografie")
        remove_choreo.setObjectName("Danger")
        remove_choreo.clicked.connect(self.remove_selected_choreography)
        edit_choreo = QPushButton("Edytuj szkielet")
        edit_choreo.clicked.connect(self.start_edit_choreography)
        self.busy_buttons.append(edit_choreo)
        refresh_button = QPushButton("Odswiez liste")
        refresh_button.clicked.connect(self.refresh_choreography_library)
        self.menu_music_button = QPushButton("Wycisz muzyke")
        self.menu_music_button.clicked.connect(self.toggle_menu_music)
        library_actions.addWidget(add_choreo)
        library_actions.addWidget(remove_choreo)
        library_actions.addWidget(edit_choreo)
        library_actions.addWidget(refresh_button)
        library_actions.addWidget(self.menu_music_button)
        library_actions.addStretch(1)
        library_layout.addLayout(library_actions)
        content.addWidget(library_box, 0, 0, 2, 1)

        preview_box = QGroupBox("Podglad")
        preview_layout = QVBoxLayout(preview_box)
        preview_box.setMouseTracking(True)
        preview_box.installEventFilter(self.preview_hover_filter)
        self.preview_box = preview_box
        self.preview_surface = QWidget()
        self.preview_surface.setMinimumSize(420, 236)
        self.preview_surface.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_surface.setMouseTracking(True)
        self.preview_surface.setProperty("previewTogglePause", True)
        self.preview_surface.installEventFilter(self.preview_hover_filter)
        preview_stack = QGridLayout(self.preview_surface)
        preview_stack.setContentsMargins(0, 0, 0, 0)
        preview_stack.setSpacing(0)

        self.preview_label = QLabel("Wybierz choreografie")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumSize(1, 1)
        self.preview_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_label.setStyleSheet("background:#070b10;border-radius:12px;color:#9fb2bf;")
        self.preview_label.setMouseTracking(True)
        self.preview_label.setProperty("previewTogglePause", True)
        self.preview_label.installEventFilter(self.preview_hover_filter)
        self.preview_label.mousePressEvent = lambda _event: self.on_preview_surface_clicked()
        preview_stack.addWidget(self.preview_label, 0, 0)

        self.preview_controls_widget = QWidget()
        self.preview_controls_widget.setMouseTracking(True)
        self.preview_controls_widget.installEventFilter(self.preview_hover_filter)
        self.preview_controls_widget.setStyleSheet(
            "background:transparent;border:0;"
            "QPushButton{background:transparent;border:0;"
            "border-radius:7px;color:#ffffff;font-size:24px;font-weight:900;padding:4px 8px;}"
            "QPushButton:hover{background:transparent;}"
            "QSlider{background:transparent;}"
            "QSlider::groove:horizontal{height:8px;background:rgba(255,255,255,135);border-radius:4px;}"
            "QSlider::sub-page:horizontal{background:#33c7a4;border-radius:3px;}"
            "QSlider::handle:horizontal{width:18px;margin:-5px 0;background:#ffffff;border-radius:9px;}"
            "QLabel{color:#ffffff;background:transparent;font-weight:800;}"
        )
        self.preview_controls_widget.setMinimumWidth(430)
        self.preview_controls_widget.hide()
        preview_controls = QHBoxLayout(self.preview_controls_widget)
        preview_controls.setContentsMargins(10, 8, 10, 8)
        preview_controls.setSpacing(10)
        self.preview_pause_button = PreviewIconButton("pause")
        self.preview_pause_button.setToolTip("Pauza / play")
        self.preview_pause_button.clicked.connect(self.toggle_preview_pause)
        self.preview_fullscreen_button = PreviewIconButton("fullscreen")
        self.preview_fullscreen_button.setToolTip("Pelny ekran")
        self.preview_fullscreen_button.clicked.connect(self.open_preview_fullscreen)
        preview_controls.addWidget(self.preview_pause_button)
        self.preview_time_slider = QSlider(Qt.Horizontal)
        self.preview_time_slider.setRange(0, 0)
        self.preview_time_slider.sliderPressed.connect(self.on_preview_timeline_pressed)
        self.preview_time_slider.sliderReleased.connect(self.on_preview_timeline_released)
        self.preview_time_slider.sliderMoved.connect(self.on_preview_timeline_moved)
        self.preview_time_label = QLabel("0:00 / 0:00")
        self.preview_time_label.setMinimumWidth(104)
        preview_controls.addWidget(self.preview_time_slider, 1)
        preview_controls.addWidget(self.preview_time_label)
        preview_controls.addWidget(self.preview_fullscreen_button)
        preview_stack.addWidget(self.preview_controls_widget, 0, 0, alignment=Qt.AlignBottom)
        preview_layout.addWidget(self.preview_surface)
        content.addWidget(preview_box, 0, 1)

        self.leaderboard_box = QGroupBox("Tabela wynikow")
        leaderboard_layout = QVBoxLayout(self.leaderboard_box)
        self.leaderboard_table = QTableWidget(0, 5)
        self.leaderboard_table.setHorizontalHeaderLabels(["Gracz", "Piosenka", "Wynik", "Ocena", "Data"])
        self.leaderboard_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.leaderboard_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.leaderboard_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.leaderboard_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.leaderboard_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.leaderboard_table.verticalHeader().setVisible(False)
        self.leaderboard_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.leaderboard_table.setEditTriggers(QTableWidget.NoEditTriggers)
        leaderboard_layout.addWidget(self.leaderboard_table)
        content.addWidget(self.leaderboard_box, 1, 1)
        page.addLayout(content, 1)

        bottom = QHBoxLayout()
        self.status_label = QLabel("Wybierz choreografie i zacznij gre.")
        self.status_label.setObjectName("Status")
        close_button = QPushButton("Zamknij")
        close_button.setObjectName("Danger")
        close_button.clicked.connect(self.close)
        bottom.addWidget(self.status_label, 1)
        bottom.addWidget(close_button)
        page.addLayout(bottom)

        self.stack.addWidget(self.main_page)

    def show_preview_controls_temporarily(self) -> None:
        if hasattr(self, "preview_controls_widget"):
            self.preview_controls_widget.show()
        if self.preview_fullscreen_controls_widget is not None:
            self.preview_fullscreen_controls_widget.show()
        self.preview_controls_hide_timer.start(3000)

    def hide_preview_controls(self) -> None:
        if self.preview_timeline_dragging:
            self.preview_controls_hide_timer.start(3000)
            return
        if hasattr(self, "preview_controls_widget"):
            self.preview_controls_widget.hide()
        if self.preview_fullscreen_controls_widget is not None:
            self.preview_fullscreen_controls_widget.hide()

    def update_preview_controls_mode(self) -> None:
        camera_mode = self.camera_preview_active
        if hasattr(self, "preview_pause_button"):
            self.preview_pause_button.setVisible(not camera_mode)
        if hasattr(self, "preview_time_slider"):
            self.preview_time_slider.setVisible(not camera_mode)
        if hasattr(self, "preview_time_label"):
            self.preview_time_label.setVisible(not camera_mode)
        if self.preview_fullscreen_pause_button is not None:
            self.preview_fullscreen_pause_button.setVisible(not camera_mode)
        if self.preview_fullscreen_time_slider is not None:
            self.preview_fullscreen_time_slider.setVisible(not camera_mode)
        if self.preview_fullscreen_time_label is not None:
            self.preview_fullscreen_time_label.setVisible(not camera_mode)

    def on_preview_surface_clicked(self) -> None:
        if self.camera_preview_active:
            return
        self.toggle_preview_pause()

    def _load_account_profiles(self) -> dict[str, dict[str, object]]:
        if not self.accounts_path.exists():
            return {}
        try:
            payload = json.loads(self.accounts_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        raw_accounts = payload.get("accounts", []) if isinstance(payload, dict) else payload
        if not isinstance(raw_accounts, list):
            return {}
        profiles: dict[str, dict[str, object]] = {}
        for item in raw_accounts:
            name = item.get("name") if isinstance(item, dict) else item
            clean_name = self.clean_account_name(name) if isinstance(name, str) else ""
            if clean_name:
                profiles[clean_name] = {
                    "name": clean_name,
                    "avatar": str(item.get("avatar", "") if isinstance(item, dict) else ""),
                    "xp": int(item.get("xp", 0) if isinstance(item, dict) else 0),
                }
        return profiles

    def _load_accounts(self) -> list[str]:
        return sorted(self._load_account_profiles(), key=str.lower)

    def _save_accounts(self, names: list[str]) -> None:
        existing_profiles = getattr(self, "account_profiles", {}) or self._load_account_profiles()
        unique_names = sorted({self.clean_account_name(name) for name in names if self.clean_account_name(name)}, key=str.lower)
        entries = []
        for name in unique_names:
            profile = existing_profiles.get(name, {})
            entries.append(
                {
                    "name": name,
                    "avatar": str(profile.get("avatar", "")),
                    "xp": int(profile.get("xp", 0) or 0),
                }
            )
        self.accounts_path.write_text(
            json.dumps({"accounts": entries}, indent=2),
            encoding="utf-8",
        )

    def _save_account_profiles(self) -> None:
        names = sorted(self.account_profiles, key=str.lower)
        entries = []
        for name in names:
            profile = self.account_profiles.get(name, {})
            entries.append(
                {
                    "name": name,
                    "avatar": str(profile.get("avatar", "")),
                    "xp": int(profile.get("xp", 0) or 0),
                }
            )
        self.accounts_path.write_text(json.dumps({"accounts": entries}, indent=2), encoding="utf-8")

    @staticmethod
    def clean_account_name(name: str) -> str:
        return " ".join(name.replace(".", "").split())

    def refresh_accounts(self) -> None:
        self.account_profiles = self._load_account_profiles()
        self.account_names = sorted(self.account_profiles, key=str.lower)
        if self.accounts_path.exists():
            self._save_account_profiles()
        if self.account_names:
            self.selected_account_index = min(self.selected_account_index, len(self.account_names) - 1)
        else:
            self.selected_account_index = 0
        self.update_account_carousel()

    def update_account_carousel(self) -> None:
        if not hasattr(self, "account_carousel"):
            return
        self.account_carousel.set_accounts(self.account_names, self.selected_account_index, self.account_profiles)
        self.previous_account_button.setEnabled(len(self.account_names) > 1)
        self.next_account_button.setEnabled(len(self.account_names) > 1)

    def on_account_carousel_selected(self, selected_index: int) -> None:
        if 0 <= selected_index < len(self.account_names):
            self.selected_account_index = selected_index

    @staticmethod
    def account_tile_styles() -> list[str]:
        return [
            "QPushButton#AccountTile { background:#2c4053; border:1px solid #52687b; color:#d5e7ee; }",
            "QPushButton#AccountTile { background:#33c7a4; border:1px solid #78ead3; color:#06241d; font-size:24px; }",
            "QPushButton#AccountTile { background:#4d3f72; border:1px solid #7467a2; color:#f0eaff; }",
        ]

    def on_account_tile_clicked(self, tile_position: int) -> None:
        if not self.account_names:
            self.open_add_account_dialog()
            return
        if tile_position == 0:
            self.previous_account()
        elif tile_position == 2:
            self.next_account()
        else:
            self.enter_app()

    def previous_account(self) -> None:
        if len(self.account_names) <= 1:
            return
        self.account_carousel.previous()

    def next_account(self) -> None:
        if len(self.account_names) <= 1:
            return
        self.account_carousel.next()

    def on_account_row_changed(self, row: int) -> None:
        if 0 <= row < len(self.account_names):
            self.selected_account_index = row

    def remember_account(self, name: str) -> None:
        clean_name = self.clean_account_name(name)
        if not clean_name:
            return
        names = self._load_accounts()
        if clean_name not in names:
            self.account_profiles[clean_name] = {"name": clean_name, "avatar": "", "xp": 0}
            self._save_account_profiles()
        self.refresh_accounts()

    def open_add_account_dialog(self) -> None:
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Dodaj konto")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 22, 22, 22)
        title = QLabel("Dodaj konto")
        title.setObjectName("Title")
        entry = QLineEdit()
        entry.setPlaceholderText("Nazwa uzytkownika")
        add_button = QPushButton("Dodaj konto")
        add_button.setObjectName("Primary")
        layout.addWidget(title)
        layout.addWidget(entry)
        layout.addWidget(add_button)

        def submit() -> None:
            name = self.clean_account_name(entry.text())
            if not name:
                QMessageBox.warning(dialog, "Brak nazwy", "Wpisz nazwe konta.")
                return
            self.remember_account(name)
            self.selected_account_index = self.account_names.index(name)
            self.update_account_carousel()
            dialog.accept()

        add_button.clicked.connect(submit)
        entry.returnPressed.connect(submit)
        dialog.resize(420, 180)
        dialog.exec()

    def delete_selected_account(self) -> None:
        if not self.account_names:
            return
        name = self.account_names[self.selected_account_index]
        remaining = [account for account in self.account_names if account != name]
        self._save_accounts(remaining)
        self.selected_account_index = max(0, min(self.selected_account_index, len(remaining) - 1))
        self.refresh_accounts()
        self.status_label.setText(f"Usunieto konto: {name}")

    def open_account_settings_dialog(self) -> None:
        target_player = self.current_player
        if not target_player and self.account_names:
            self.selected_account_index = max(0, min(self.selected_account_index, len(self.account_names) - 1))
            target_player = self.account_names[self.selected_account_index]
        if not target_player:
            QMessageBox.warning(self.window, "Brak konta", "Wybierz konto przed edycja ustawien.")
            return
        profile = self.account_profiles.get(target_player, {"avatar": "", "xp": 0})
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Ustawienia konta")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 22, 22, 22)

        title = QLabel("Ustawienia konta")
        title.setObjectName("Title")
        name_entry = QLineEdit(target_player)
        avatar_label = QLabel(str(profile.get("avatar", "")) or "Brak grafiki")
        avatar_label.setObjectName("Subtitle")
        pick_avatar_button = QPushButton("Wybierz grafike")
        save_button = QPushButton("Zapisz")
        save_button.setObjectName("Primary")
        avatar_path = {"value": str(profile.get("avatar", ""))}

        def pick_avatar() -> None:
            path, _ = QFileDialog.getOpenFileName(
                dialog,
                "Wybierz grafike profilu",
                "",
                "Images (*.png *.jpg *.jpeg *.bmp *.webp);;All files (*.*)",
            )
            if path:
                avatar_path["value"] = path
                avatar_label.setText(path)

        def save() -> None:
            new_name = self.clean_account_name(name_entry.text())
            if not new_name:
                QMessageBox.warning(dialog, "Brak nazwy", "Nazwa gracza nie moze byc pusta.")
                return
            if new_name != target_player and new_name in self.account_profiles:
                QMessageBox.warning(dialog, "Nazwa zajeta", "Takie konto juz istnieje.")
                return

            old_name = target_player
            updated_profile = dict(self.account_profiles.get(old_name, {}))
            updated_profile["name"] = new_name
            updated_profile["avatar"] = avatar_path["value"]
            updated_profile["xp"] = int(updated_profile.get("xp", 0) or 0)
            if new_name != old_name:
                self.account_profiles.pop(old_name, None)
            self.account_profiles[new_name] = updated_profile
            if self.current_player == old_name:
                self.current_player = new_name
            self.account_names = sorted(self.account_profiles, key=str.lower)
            self.selected_account_index = self.account_names.index(new_name)
            self._save_account_profiles()
            self.refresh_accounts()
            self.update_player_badge()
            dialog.accept()

        pick_avatar_button.clicked.connect(pick_avatar)
        save_button.clicked.connect(save)
        layout.addWidget(title)
        layout.addWidget(QLabel("Nazwa gracza"))
        layout.addWidget(name_entry)
        layout.addWidget(QLabel("Grafika profilu"))
        layout.addWidget(avatar_label)
        layout.addWidget(pick_avatar_button)
        layout.addWidget(save_button)
        dialog.resize(560, 320)
        dialog.exec()

    def enter_app(self) -> None:
        if not self.account_names:
            QMessageBox.warning(self.window, "Brak konta", "Dodaj konto przed startem.")
            return
        self.selected_account_index = max(0, min(self.selected_account_index, len(self.account_names) - 1))
        self.current_player = self.account_names[self.selected_account_index]
        self.login_audio_timer.stop()
        self.stop_menu_audio()
        if isinstance(self.login_page, VideoBackground):
            self.login_page.stop_video()
        self.update_player_badge()
        self.stack.setCurrentWidget(self.main_page)
        self.start_hand_menu_control()
        self.on_choreography_selected()

    def change_account(self) -> None:
        self.stop_inline_camera_preview(restore_choreography=False, restart_hand_control=False)
        self.stop_menu_preview()
        self.refresh_accounts()
        self.update_login_background()
        self.stack.setCurrentWidget(self.login_page)
        self.start_login_background_audio()
        self.start_hand_menu_control()

    def update_player_badge(self) -> None:
        if not self.current_player:
            self.player_badge.setText("")
            self.player_avatar_label.clear()
            return
        profile = self.account_profiles.get(self.current_player, {})
        xp = int(profile.get("xp", 0) or 0)
        self.player_badge.setText(f"Grasz jako: {self.current_player}  |  LVL {account_level(xp)}  |  {xp} pkt")
        self.player_avatar_label.setPixmap(self.profile_avatar_pixmap(self.current_player, 42))

    def profile_avatar_pixmap(self, name: str, size: int) -> QPixmap:
        profile = self.account_profiles.get(name, {})
        avatar_path = str(profile.get("avatar", ""))
        has_avatar = bool(avatar_path and Path(avatar_path).exists())
        source = QPixmap(avatar_path) if has_avatar else QPixmap(size, size)
        if source.isNull() or not has_avatar:
            has_avatar = False
            source = QPixmap(size, size)
            source.fill(QColor("#33c7a4"))
        scaled = source.scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        result = QPixmap(size, size)
        result.fill(Qt.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addEllipse(0, 0, size, size)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, scaled)
        if not has_avatar:
            painter.setClipping(False)
            painter.setPen(QColor("#071018"))
            painter.setFont(QFont("Segoe UI", max(12, int(size * 0.42)), QFont.Black))
            painter.drawText(result.rect(), Qt.AlignCenter, name[:1].upper() if name else "?")
        painter.end()
        return result

    def refresh_camera_choices(self) -> None:
        options = list_available_cameras()
        self.camera_choices = {label: index for label, index in options}
        self.camera_combo.clear()
        for label in self.camera_choices:
            self.camera_combo.addItem(label)

    def get_selected_camera_index(self) -> int:
        label = self.camera_combo.currentText()
        if label in self.camera_choices:
            return self.camera_choices[label]
        return 0

    def build_camera_config(self, difficulty: str = DEFAULT_DIFFICULTY) -> AppConfig:
        return AppConfig(
            camera_index=self.get_selected_camera_index(),
            width=self.camera_width,
            height=self.camera_height,
            camera_fps=self.camera_fps,
            prefer_mjpg=self.camera_prefer_mjpg,
            pose_model_complexity=self.pose_model_complexity,
            min_detection_confidence=self.min_detection_confidence,
            min_tracking_confidence=self.min_tracking_confidence,
            mirror_view=self.mirror_check.isChecked(),
            fullscreen=self.game_fullscreen,
            difficulty=difficulty,
        )

    def open_camera_settings_dialog(self) -> None:
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Wiecej ustawien kamerki")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        title = QLabel("Wiecej ustawien kamerki")
        title.setObjectName("Title")
        resolution_combo = QComboBox()
        resolution_options = [
            ("640 x 480", 640, 480),
            ("960 x 540", 960, 540),
            ("1280 x 720", 1280, 720),
            ("1920 x 1080", 1920, 1080),
        ]
        for label, width, height in resolution_options:
            resolution_combo.addItem(label, (width, height))
        current_resolution_index = next(
            (
                index
                for index, (_label, width, height) in enumerate(resolution_options)
                if width == self.camera_width and height == self.camera_height
            ),
            0,
        )
        resolution_combo.setCurrentIndex(current_resolution_index)

        fps_spin = QSpinBox()
        fps_spin.setRange(5, 120)
        fps_spin.setSingleStep(5)
        fps_spin.setValue(self.camera_fps)

        model_combo = QComboBox()
        model_combo.addItem("Szybki", 0)
        model_combo.addItem("Dokladny", 1)
        model_combo.addItem("Najdokladniejszy", 2)
        model_combo.setCurrentIndex(max(0, min(2, self.pose_model_complexity)))

        detection_spin = QDoubleSpinBox()
        detection_spin.setRange(0.1, 0.95)
        detection_spin.setSingleStep(0.05)
        detection_spin.setDecimals(2)
        detection_spin.setValue(self.min_detection_confidence)

        tracking_spin = QDoubleSpinBox()
        tracking_spin.setRange(0.1, 0.95)
        tracking_spin.setSingleStep(0.05)
        tracking_spin.setDecimals(2)
        tracking_spin.setValue(self.min_tracking_confidence)

        mjpg_check = QCheckBox("Preferuj tryb MJPG")
        mjpg_check.setChecked(self.camera_prefer_mjpg)
        fullscreen_check = QCheckBox("Gra w trybie pelnoekranowym")
        fullscreen_check.setChecked(self.game_fullscreen)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.addWidget(QLabel("Rozdzielczosc"), 0, 0)
        grid.addWidget(resolution_combo, 0, 1)
        grid.addWidget(QLabel("FPS kamerki"), 1, 0)
        grid.addWidget(fps_spin, 1, 1)
        grid.addWidget(QLabel("Model pozy"), 2, 0)
        grid.addWidget(model_combo, 2, 1)
        grid.addWidget(QLabel("Pewnosc wykrywania"), 3, 0)
        grid.addWidget(detection_spin, 3, 1)
        grid.addWidget(QLabel("Pewnosc sledzenia"), 4, 0)
        grid.addWidget(tracking_spin, 4, 1)
        grid.addWidget(mjpg_check, 5, 1)
        grid.addWidget(fullscreen_check, 6, 1)

        save_button = QPushButton("Zapisz")
        save_button.setObjectName("Primary")
        cancel_button = QPushButton("Anuluj")
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel_button)
        buttons.addWidget(save_button)

        layout.addWidget(title)
        layout.addLayout(grid)
        layout.addLayout(buttons)

        def submit() -> None:
            width, height = resolution_combo.currentData()
            self.camera_width = int(width)
            self.camera_height = int(height)
            self.camera_fps = int(fps_spin.value())
            self.pose_model_complexity = int(model_combo.currentData())
            self.min_detection_confidence = float(detection_spin.value())
            self.min_tracking_confidence = float(tracking_spin.value())
            self.camera_prefer_mjpg = mjpg_check.isChecked()
            self.game_fullscreen = fullscreen_check.isChecked()
            dialog.accept()
            self.status_label.setText(
                f"Kamerka: {self.camera_width}x{self.camera_height} @ {self.camera_fps} FPS"
            )
            if self.camera_preview_active:
                self.stop_inline_camera_preview(restore_choreography=False, restart_hand_control=False)
                self.start_camera_preview()

        cancel_button.clicked.connect(dialog.reject)
        save_button.clicked.connect(submit)
        dialog.resize(520, 380)
        dialog.exec()

    def _load_remembered_choreography_paths(self) -> list[Path]:
        if not self.library_path.exists():
            return []
        try:
            payload = json.loads(self.library_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        raw_paths = payload.get("choreographies", []) if isinstance(payload, dict) else payload
        if not isinstance(raw_paths, list):
            return []
        paths = []
        for item in raw_paths:
            path_value = item.get("path") if isinstance(item, dict) else item
            if isinstance(path_value, str) and path_value.strip():
                paths.append(Path(path_value))
        return paths

    def _save_remembered_choreography_paths(self, paths: list[Path]) -> None:
        seen = set()
        entries = []
        for path in paths:
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            entries.append({"path": resolved})
        self.library_path.write_text(json.dumps({"choreographies": entries}, indent=2), encoding="utf-8")

    def _remember_choreography_path(self, path: Path) -> None:
        paths = self._load_remembered_choreography_paths()
        paths.append(path.resolve())
        self._save_remembered_choreography_paths(paths)

    def _load_hidden_choreography_paths(self) -> set[str]:
        if not self.hidden_choreographies_path.exists():
            return set()
        try:
            payload = json.loads(self.hidden_choreographies_path.read_text(encoding="utf-8"))
        except Exception:
            return set()
        raw_paths = payload.get("hidden", []) if isinstance(payload, dict) else payload
        if not isinstance(raw_paths, list):
            return set()
        hidden_paths = set()
        for path_value in raw_paths:
            if isinstance(path_value, str) and path_value.strip():
                hidden_paths.add(str(Path(path_value).resolve()))
        return hidden_paths

    def _save_hidden_choreography_paths(self, hidden_paths: set[str]) -> None:
        payload = {"hidden": sorted(hidden_paths)}
        self.hidden_choreographies_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _hide_choreography_path(self, path: Path) -> None:
        hidden_paths = self._load_hidden_choreography_paths()
        hidden_paths.add(str(path.resolve()))
        self._save_hidden_choreography_paths(hidden_paths)

    def _unhide_choreography_path(self, path: Path) -> None:
        hidden_paths = self._load_hidden_choreography_paths()
        resolved = str(path.resolve())
        if resolved in hidden_paths:
            hidden_paths.remove(resolved)
            self._save_hidden_choreography_paths(hidden_paths)

    def refresh_choreography_library(self) -> None:
        self.stop_menu_preview()
        self.choreography_list.clear()
        self.choreography_items.clear()
        hidden_paths = self._load_hidden_choreography_paths()
        json_paths = []
        for json_path in sorted(Path.cwd().rglob("*.json")):
            relative_path = json_path.relative_to(Path.cwd())
            if any(part.startswith(".") for part in relative_path.parts):
                continue
            if json_path.resolve() in {
                self.library_path,
                self.leaderboard_path,
                self.accounts_path,
                self.hidden_choreographies_path,
            }:
                continue
            json_paths.append(json_path)
        json_paths.extend(self._load_remembered_choreography_paths())

        seen = set()
        for json_path in json_paths:
            resolved = json_path.resolve()
            item_id = str(resolved)
            if item_id in seen or not resolved.exists():
                continue
            if item_id in hidden_paths:
                continue
            seen.add(item_id)
            try:
                data = load_choreography(resolved)
            except Exception:
                continue
            source = Path(data.source_video)
            if not source.is_absolute():
                source = (resolved.parent / source).resolve()
            title = source.stem if source.name else resolved.stem
            duration = self.format_duration(data.duration_seconds)
            item = QListWidgetItem(f"{title}\n{duration}  |  {data.frame_count} pozy")
            item.setData(Qt.UserRole, item_id)
            item.setSizeHint(QSize(240, 72))
            self.choreography_items[item_id] = {"json": item_id, "source_video": str(source)}
            self.choreography_list.addItem(item)

        if self.choreography_list.count():
            self.choreography_list.setCurrentRow(0)
        else:
            self.preview_label.setText("Brak choreografii")
            self.status_label.setText("Brak choreografii. Dodaj JSON albo zeskanuj film.")

    def add_choreography_to_library(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self.window,
            "Dodaj choreografie do listy",
            "",
            "JSON files (*.json);;All files (*.*)",
        )
        if not path:
            return
        json_path = Path(path).resolve()
        try:
            load_choreography(json_path)
        except Exception as exc:
            QMessageBox.critical(self.window, "Bledny plik", f"Nie udalo sie wczytac JSON:\n{exc}")
            return
        self._unhide_choreography_path(json_path)
        self._remember_choreography_path(json_path)
        self.refresh_choreography_library()
        self.select_choreography_path(json_path)

    def remove_selected_choreography(self) -> None:
        item = self.choreography_list.currentItem()
        if item is None:
            QMessageBox.warning(self.window, "Brak wyboru", "Wybierz choreografie do usuniecia z gry.")
            return
        choreography_path = Path(str(item.data(Qt.UserRole))).resolve()
        title = item.text().splitlines()[0] if item.text() else choreography_path.stem
        answer = QMessageBox.question(
            self.window,
            "Usun choreografie",
            f"Usunac '{title}' z listy choreografii w grze?\nPlik JSON zostanie na dysku.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        remembered_paths = [
            path for path in self._load_remembered_choreography_paths()
            if path.resolve() != choreography_path
        ]
        self._save_remembered_choreography_paths(remembered_paths)
        self._hide_choreography_path(choreography_path)
        self.refresh_choreography_library()
        self.status_label.setText(f"Usunieto choreografie z gry: {title}")

    def select_choreography_path(self, path: Path) -> None:
        item_id = str(path.resolve())
        for row in range(self.choreography_list.count()):
            item = self.choreography_list.item(row)
            if item.data(Qt.UserRole) == item_id:
                self.choreography_list.setCurrentRow(row)
                break

    def update_login_background(self) -> None:
        if not isinstance(self.login_page, VideoBackground):
            return
        self.login_page.set_video_path(self.find_login_background_video())

    def start_login_background_audio(self) -> None:
        if not isinstance(self.login_page, VideoBackground):
            return
        video_path = self.login_page.video_path
        if video_path is not None and video_path.exists():
            if self.login_page.has_combined_media():
                self.stop_menu_audio()
                self.login_page.play_video(self.menu_music_muted)
                self.login_audio_timer.stop()
                return
            self.login_page.restart_from_beginning()
            self.start_menu_audio(video_path, 0.0)
            self.login_audio_timer.start(500)

    def start_hand_menu_control(self) -> None:
        if self.hand_menu_controller is not None and not self.busy:
            self.hand_menu_controller.start()

    def stop_hand_menu_control(self) -> None:
        if self.hand_menu_controller is not None:
            self.hand_menu_controller.stop()

    def sync_login_background_audio(self) -> None:
        if self.stack.currentWidget() != self.login_page:
            self.login_audio_timer.stop()
            return
        if not isinstance(self.login_page, VideoBackground):
            return
        if self.login_page.has_combined_media():
            self.login_audio_timer.stop()
            return
        video_path = self.login_page.video_path
        if video_path is None or not video_path.exists():
            return
        if self.menu_audio_player is None or not self.menu_audio_player.is_playing():
            self.start_menu_audio(video_path, self.login_page.current_time_seconds())

    def find_login_background_video(self) -> Path | None:
        entries = self._load_leaderboard_entries()
        entries.sort(key=lambda entry: str(entry.get("date", "")), reverse=True)
        for entry in entries:
            choreography_value = str(entry.get("choreography", "")).strip()
            if not choreography_value:
                continue
            video_path = self.video_path_for_choreography(Path(choreography_value))
            if video_path is not None:
                return video_path

        for item in self.choreography_items.values():
            video_path = Path(item["source_video"])
            if video_path.exists():
                return video_path
        return None

    @staticmethod
    def video_path_for_choreography(choreography_path: Path) -> Path | None:
        if not choreography_path.exists():
            return None
        try:
            data = load_choreography(choreography_path)
        except Exception:
            return None
        source = Path(data.source_video)
        if not source.is_absolute():
            source = (choreography_path.parent / source).resolve()
        return source if source.exists() else None

    def on_choreography_selected(self) -> None:
        item = self.choreography_list.currentItem()
        if item is None:
            return
        item_id = item.data(Qt.UserRole)
        data = self.choreography_items.get(item_id)
        if not data:
            return
        self.status_label.setText(f"Wybrano choreografie: {Path(data['json']).name}")
        self.refresh_leaderboard(Path(data["json"]))
        if self.stack.currentWidget() == self.main_page:
            self.start_menu_preview(Path(data["source_video"]))

    @staticmethod
    def format_duration(seconds: float) -> str:
        total = max(0, int(round(seconds)))
        return f"{total // 60}:{total % 60:02d}"

    @staticmethod
    def format_time_ms(milliseconds: int) -> str:
        total = max(0, int(round(milliseconds / 1000.0)))
        return f"{total // 60}:{total % 60:02d}"

    def set_preview_timeline_enabled(self, enabled: bool) -> None:
        if not hasattr(self, "preview_time_slider"):
            return
        self.preview_time_slider.setEnabled(enabled)
        if self.preview_fullscreen_time_slider is not None:
            self.preview_fullscreen_time_slider.setEnabled(enabled)
        if not enabled:
            self.preview_timeline_duration_ms = 0
            self.preview_time_slider.blockSignals(True)
            self.preview_time_slider.setRange(0, 0)
            self.preview_time_slider.setValue(0)
            self.preview_time_slider.blockSignals(False)
            if self.preview_fullscreen_time_slider is not None:
                self.preview_fullscreen_time_slider.blockSignals(True)
                self.preview_fullscreen_time_slider.setRange(0, 0)
                self.preview_fullscreen_time_slider.setValue(0)
                self.preview_fullscreen_time_slider.blockSignals(False)
            self.preview_time_label.setText("0:00 / 0:00")

    def set_preview_timeline_duration(self, duration_ms: int) -> None:
        self.preview_timeline_duration_ms = max(0, int(duration_ms))
        self.preview_time_slider.blockSignals(True)
        self.preview_time_slider.setRange(0, self.preview_timeline_duration_ms)
        self.preview_time_slider.setValue(0)
        self.preview_time_slider.blockSignals(False)
        self.preview_time_slider.setEnabled(self.preview_timeline_duration_ms > 0)
        if self.preview_fullscreen_time_slider is not None:
            self.preview_fullscreen_time_slider.blockSignals(True)
            self.preview_fullscreen_time_slider.setRange(0, self.preview_timeline_duration_ms)
            self.preview_fullscreen_time_slider.setValue(0)
            self.preview_fullscreen_time_slider.blockSignals(False)
            self.preview_fullscreen_time_slider.setEnabled(self.preview_timeline_duration_ms > 0)
        self.update_preview_time_label(0)

    def update_preview_time_label(self, position_ms: int) -> None:
        duration_ms = max(0, self.preview_timeline_duration_ms)
        position_ms = max(0, min(int(position_ms), duration_ms if duration_ms else int(position_ms)))
        remaining_ms = max(0, duration_ms - position_ms)
        time_text = f"{self.format_time_ms(position_ms)} / -{self.format_time_ms(remaining_ms)}"
        self.preview_time_label.setText(time_text)
        if self.preview_fullscreen_time_label is not None:
            self.preview_fullscreen_time_label.setText(time_text)

    def set_preview_timeline_position(self, position_ms: int) -> None:
        if self.preview_timeline_dragging or not hasattr(self, "preview_time_slider"):
            return
        position_ms = max(0, min(int(position_ms), self.preview_timeline_duration_ms))
        self.preview_time_slider.blockSignals(True)
        self.preview_time_slider.setValue(position_ms)
        self.preview_time_slider.blockSignals(False)
        if self.preview_fullscreen_time_slider is not None:
            self.preview_fullscreen_time_slider.blockSignals(True)
            self.preview_fullscreen_time_slider.setValue(position_ms)
            self.preview_fullscreen_time_slider.blockSignals(False)
        self.update_preview_time_label(position_ms)

    def set_preview_pause_icon(self) -> None:
        icon = "play" if self.preview_paused else "pause"
        if hasattr(self, "preview_pause_button"):
            self.preview_pause_button.set_icon_kind(icon)
        if self.preview_fullscreen_pause_button is not None:
            self.preview_fullscreen_pause_button.set_icon_kind(icon)

    def display_preview_pixmap(self, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            return
        self.preview_source_pixmap = pixmap
        self.preview_label.setPixmap(
            pixmap.scaled(self.preview_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )
        if self.preview_fullscreen_label is not None:
            self.preview_fullscreen_label.setPixmap(
                pixmap.scaled(
                    self.preview_fullscreen_label.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )

    def toggle_preview_pause(self) -> None:
        if self.preview_source_pixmap is None and not self.preview_video_path and not self.camera_preview_active:
            return
        self.preview_paused = not self.preview_paused
        self.set_preview_pause_icon()
        if self.camera_preview_active:
            if self.preview_paused:
                self.camera_preview_timer.stop()
            else:
                self.camera_preview_previous_time = time.perf_counter()
                self.camera_preview_timer.start(33)
            return
        if self.preview_media_player is not None and self.preview_video_path is not None and self.has_combined_preview_media():
            if self.preview_paused:
                self.preview_media_player.pause()
            else:
                self.preview_media_player.play()
            return
        if self.preview_cap is not None:
            if self.preview_paused:
                self.preview_timer.stop()
                self.stop_menu_audio()
            else:
                position_ms = self.preview_time_slider.value() if hasattr(self, "preview_time_slider") else 0
                self.seek_fallback_menu_preview(position_ms, restart_audio=True)
                self.preview_timer.start(self.preview_delay_ms)

    def open_preview_fullscreen(self) -> None:
        if self.preview_source_pixmap is None:
            return
        if self.preview_fullscreen_dialog is not None:
            self.preview_fullscreen_dialog.raise_()
            return
        dialog = QDialog()
        dialog.setWindowTitle("Podglad")
        dialog.setModal(False)
        dialog.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        dialog.setStyleSheet("background:#000000;")
        layout = QGridLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        label = QLabel()
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("background:#000000;")
        label.setMouseTracking(True)
        label.setProperty("previewTogglePause", True)
        label.installEventFilter(self.preview_hover_filter)
        label.mousePressEvent = lambda _event: self.on_preview_surface_clicked()
        layout.addWidget(label, 0, 0)

        controls = QWidget()
        controls.setMouseTracking(True)
        controls.installEventFilter(self.preview_hover_filter)
        controls.setStyleSheet(
            "background:transparent;border:0;"
            "QPushButton{background:transparent;border:0;"
            "border-radius:7px;color:#ffffff;font-size:24px;font-weight:900;padding:6px 8px;}"
            "QPushButton:hover{background:transparent;}"
            "QSlider{background:transparent;}"
            "QSlider::groove:horizontal{height:8px;background:rgba(255,255,255,135);border-radius:4px;}"
            "QSlider::sub-page:horizontal{background:#33c7a4;border-radius:4px;}"
            "QSlider::handle:horizontal{width:18px;margin:-5px 0;background:#ffffff;border-radius:9px;}"
            "QLabel{color:#ffffff;background:transparent;font-weight:800;font-size:15px;}"
        )
        controls.setMinimumWidth(640)
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(14, 10, 14, 10)
        controls_layout.setSpacing(12)
        pause_button = PreviewIconButton("play" if self.preview_paused else "pause")
        pause_button.setFixedSize(46, 40)
        pause_button.clicked.connect(self.toggle_preview_pause)
        time_slider = QSlider(Qt.Horizontal)
        time_slider.setRange(0, self.preview_timeline_duration_ms)
        time_slider.setValue(self.preview_time_slider.value())
        time_slider.setEnabled(self.preview_time_slider.isEnabled())
        time_slider.sliderPressed.connect(self.on_preview_timeline_pressed)
        time_slider.sliderMoved.connect(self.on_preview_timeline_moved)
        time_slider.sliderReleased.connect(lambda: self.on_preview_timeline_released_from(time_slider.value()))
        time_label = QLabel(self.preview_time_label.text())
        time_label.setMinimumWidth(112)
        close_button = PreviewIconButton("fullscreen")
        close_button.setFixedSize(46, 40)
        close_button.clicked.connect(dialog.close)
        controls_layout.addWidget(pause_button)
        controls_layout.addWidget(time_slider, 1)
        controls_layout.addWidget(time_label)
        controls_layout.addWidget(close_button)
        layout.addWidget(controls, 0, 0, alignment=Qt.AlignBottom)

        def close_fullscreen() -> None:
            self.preview_fullscreen_dialog = None
            self.preview_fullscreen_label = None
            self.preview_fullscreen_controls_widget = None
            self.preview_fullscreen_pause_button = None
            self.preview_fullscreen_time_slider = None
            self.preview_fullscreen_time_label = None

        dialog.finished.connect(lambda _result: close_fullscreen())
        self.preview_fullscreen_dialog = dialog
        self.preview_fullscreen_label = label
        self.preview_fullscreen_controls_widget = controls
        self.preview_fullscreen_pause_button = pause_button
        self.preview_fullscreen_time_slider = time_slider
        self.preview_fullscreen_time_label = time_label
        self.update_preview_controls_mode()
        self.display_preview_pixmap(self.preview_source_pixmap)
        dialog.showFullScreen()
        QTimer.singleShot(0, lambda: self.display_preview_pixmap(self.preview_source_pixmap) if self.preview_source_pixmap else None)
        self.show_preview_controls_temporarily()

    def on_preview_timeline_pressed(self) -> None:
        self.preview_timeline_dragging = True
        if self.preview_media_player is not None and self.preview_video_path is not None:
            self.preview_media_player.pause()
        elif self.preview_cap is not None:
            self.stop_menu_audio()

    def on_preview_timeline_moved(self, value: int) -> None:
        self.update_preview_time_label(value)
        if self.preview_cap is not None and self.preview_fps > 0:
            self.seek_fallback_menu_preview(value, restart_audio=False)

    def on_preview_timeline_released(self) -> None:
        self.on_preview_timeline_released_from(self.preview_time_slider.value())

    def on_preview_timeline_released_from(self, value: int) -> None:
        self.preview_timeline_dragging = False
        self.seek_menu_preview(value)

    def seek_menu_preview(self, position_ms: int) -> None:
        position_ms = max(0, min(int(position_ms), self.preview_timeline_duration_ms))
        if self.preview_media_player is not None and self.preview_video_path is not None and self.has_combined_preview_media():
            self.preview_media_player.setPosition(position_ms)
            if not self.preview_paused:
                self.preview_media_player.play()
            self.update_preview_time_label(position_ms)
            return
        if self.preview_cap is not None:
            self.seek_fallback_menu_preview(position_ms, restart_audio=not self.preview_paused)

    def seek_fallback_menu_preview(self, position_ms: int, restart_audio: bool) -> None:
        if self.preview_cap is None:
            return
        seconds = max(0.0, position_ms / 1000.0)
        self.preview_started_at = time.perf_counter() - seconds
        self.preview_cap.set(cv2.CAP_PROP_POS_MSEC, position_ms)
        ok, frame = self.preview_cap.read()
        if ok:
            self.display_preview_pixmap(self.frame_to_pixmap(frame))
        if restart_audio and self.preview_video_path is not None:
            self.start_menu_audio(self.preview_video_path, seconds)
        self.set_preview_timeline_position(position_ms)

    def on_preview_media_position_changed(self, position_ms: int) -> None:
        self.set_preview_timeline_position(position_ms)

    def on_preview_media_duration_changed(self, duration_ms: int) -> None:
        if self.preview_video_path is not None and self.has_combined_preview_media():
            self.set_preview_timeline_duration(duration_ms)

    def start_menu_preview(self, video_path: Path) -> None:
        if self.camera_preview_active:
            self.stop_inline_camera_preview(restore_choreography=False)
        self.stop_menu_preview()
        self.preview_paused = False
        if hasattr(self, "preview_pause_button"):
            self.set_preview_pause_icon()
        self.update_preview_controls_mode()
        self.preview_video_path = video_path
        self.set_preview_timeline_enabled(False)
        if not video_path.exists():
            self.preview_label.setText("Brak filmu")
            return
        if self.has_combined_preview_media():
            self.preview_audio_output.setMuted(self.menu_music_muted)
            self.preview_media_player.setSource(QUrl.fromLocalFile(str(video_path)))
            self.preview_media_player.setPosition(0)
            self.preview_media_player.play()
            self.refresh_combined_preview_timeline()
            QTimer.singleShot(150, self.refresh_combined_preview_timeline)
            self.preview_label.setText("")
            return
        self.preview_cap = cv2.VideoCapture(str(video_path))
        if not self.preview_cap.isOpened():
            self.preview_cap.release()
            self.preview_cap = None
            self.preview_label.setText("Brak podgladu")
            return
        self.preview_fps = self.preview_cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.preview_frame_count = int(self.preview_cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.preview_duration_seconds = (
            self.preview_frame_count / self.preview_fps
            if self.preview_frame_count and self.preview_fps
            else 0.0
        )
        self.set_preview_timeline_duration(int(self.preview_duration_seconds * 1000))
        self.preview_delay_ms = max(16, int(round(1000.0 / max(self.preview_fps, 1.0))))
        self.preview_started_at = time.perf_counter()
        self.start_menu_audio(video_path)
        self.preview_timer.start(self.preview_delay_ms)
        self.update_menu_preview_frame()

    def has_combined_preview_media(self) -> bool:
        return (
            self.preview_media_player is not None
            and self.preview_audio_output is not None
            and self.preview_video_sink is not None
        )

    def refresh_combined_preview_timeline(self) -> None:
        if self.preview_media_player is None or self.preview_video_path is None or not self.has_combined_preview_media():
            return
        duration_ms = int(self.preview_media_player.duration() or 0)
        if duration_ms > 0:
            if self.preview_timeline_duration_ms != duration_ms:
                self.set_preview_timeline_duration(duration_ms)
            else:
                self.preview_time_slider.setEnabled(True)
                if self.preview_fullscreen_time_slider is not None:
                    self.preview_fullscreen_time_slider.setEnabled(True)
            self.set_preview_timeline_position(int(self.preview_media_player.position() or 0))
        self.update_preview_controls_mode()

    def on_preview_media_frame_changed(self, frame) -> None:
        image = frame.toImage()
        if image.isNull():
            return
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            return
        self.display_preview_pixmap(pixmap)

    def on_preview_media_status_changed(self, status) -> None:
        if self.preview_media_player is None or self.preview_video_path is None:
            return
        if getattr(status, "name", "") == "EndOfMedia":
            self.preview_media_player.setPosition(0)
            self.preview_media_player.play()

    def start_menu_audio(self, video_path: Path, start_seconds: float = 0.0) -> None:
        self.stop_menu_audio()
        self.menu_audio_player = AudioPlayer(video_path, prefer_python_vlc=True)
        self.menu_audio_player.set_muted(self.menu_music_muted)
        self.menu_audio_player.play(start_seconds)

    def stop_menu_audio(self) -> None:
        if self.menu_audio_player is not None:
            self.menu_audio_player.stop()
            self.menu_audio_player = None

    def toggle_menu_music(self) -> None:
        self.menu_music_muted = not self.menu_music_muted
        self.menu_music_button.setText("Wlacz muzyke" if self.menu_music_muted else "Wycisz muzyke")
        if isinstance(self.login_page, VideoBackground):
            self.login_page.set_muted(self.menu_music_muted)
        if self.preview_audio_output is not None:
            self.preview_audio_output.setMuted(self.menu_music_muted)
        if self.menu_audio_player is not None:
            self.menu_audio_player.set_muted(self.menu_music_muted)

    def update_menu_preview_frame(self) -> None:
        if self.preview_cap is None:
            return

        ok, frame = self.preview_cap.read()
        if not ok:
            self.preview_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            self.preview_started_at = time.perf_counter()
            if self.preview_video_path is not None:
                self.start_menu_audio(self.preview_video_path)
            ok, frame = self.preview_cap.read()
            if not ok:
                self.stop_menu_preview()
                self.preview_label.setText("Brak podgladu")
                return
        self.display_preview_pixmap(self.frame_to_pixmap(frame))
        position_ms = int(self.preview_cap.get(cv2.CAP_PROP_POS_MSEC) or 0)
        self.set_preview_timeline_position(position_ms)

    @staticmethod
    def frame_to_pixmap(frame) -> QPixmap:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width, channels = rgb.shape
        image = QImage(rgb.data, width, height, channels * width, QImage.Format_RGB888).copy()
        return QPixmap.fromImage(image)

    def stop_menu_preview(self) -> None:
        self.stop_menu_audio()
        self.preview_timer.stop()
        if self.preview_media_player is not None:
            self.preview_media_player.stop()
        if self.preview_cap is not None:
            self.preview_cap.release()
            self.preview_cap = None
        self.preview_video_path = None
        self.preview_source_pixmap = None
        self.preview_paused = False
        if hasattr(self, "preview_pause_button"):
            self.set_preview_pause_icon()
        if self.preview_fullscreen_dialog is not None:
            self.preview_fullscreen_dialog.close()
        self.set_preview_timeline_enabled(False)
        self.preview_started_at = 0.0
        self.preview_frame_count = 0
        self.preview_duration_seconds = 0.0

    def open_scan_dialog(self) -> None:
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Skanuj choreografie")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 24)
        title = QLabel("Skanuj choreografie")
        title.setObjectName("Title")
        video_entry = QLineEdit(self.scan_video_path)
        output_entry = QLineEdit(self.scan_output_path)
        fps_spin = QSpinBox()
        fps_spin.setRange(1, 60)
        fps_spin.setValue(10)
        pick_video_button = QPushButton("Wybierz film")
        pick_output_button = QPushButton("Zapisz jako")
        scan_button = QPushButton("Skanuj choreografie")
        scan_button.setObjectName("Primary")
        cancel_button = QPushButton("Anuluj")

        grid = QGridLayout()
        grid.addWidget(QLabel("Film do skanowania"), 0, 0)
        grid.addWidget(video_entry, 0, 1)
        grid.addWidget(pick_video_button, 0, 2)
        grid.addWidget(QLabel("Plik wyjsciowy JSON"), 1, 0)
        grid.addWidget(output_entry, 1, 1)
        grid.addWidget(pick_output_button, 1, 2)
        grid.addWidget(QLabel("FPS skanowania"), 2, 0)
        grid.addWidget(fps_spin, 2, 1)
        buttons = QHBoxLayout()
        buttons.addWidget(cancel_button)
        buttons.addWidget(scan_button)
        layout.addWidget(title)
        layout.addLayout(grid)
        layout.addLayout(buttons)

        def pick_video() -> None:
            path, _ = QFileDialog.getOpenFileName(
                dialog,
                "Wybierz film do skanowania",
                "",
                "Video files (*.mp4 *.mov *.avi *.mkv);;All files (*.*)",
            )
            if path:
                video_entry.setText(path)
                output_entry.setText(str(Path(path).with_suffix(".json")))

        def pick_output() -> None:
            path, _ = QFileDialog.getSaveFileName(dialog, "Wybierz plik JSON", "", "JSON files (*.json)")
            if path:
                output_entry.setText(path)

        def submit() -> None:
            self.scan_video_path = video_entry.text().strip()
            self.scan_output_path = output_entry.text().strip()
            dialog.accept()
            self.start_scan(Path(self.scan_video_path), Path(self.scan_output_path), float(fps_spin.value()))

        pick_video_button.clicked.connect(pick_video)
        pick_output_button.clicked.connect(pick_output)
        cancel_button.clicked.connect(dialog.reject)
        scan_button.clicked.connect(submit)
        dialog.resize(760, 300)
        dialog.exec()

    def start_camera_preview(self) -> None:
        if self.busy:
            return
        if self.camera_preview_active:
            self.stop_inline_camera_preview(restore_choreography=True)
            return

        config = self.build_camera_config()
        self.stop_hand_menu_control()
        self.stop_menu_preview()
        cap = open_camera(
            config.camera_index,
            config.width,
            config.height,
            config.camera_fps,
            config.prefer_mjpg,
        )
        if cap is None:
            QMessageBox.critical(
                self.window,
                "Brak kamerki",
                "Nie udalo sie otworzyc kamerki. Sprawdz, czy nie jest zajeta przez inna aplikacje.",
            )
            self.start_hand_menu_control()
            self.on_choreography_selected()
            return

        self.camera_preview_cap = cap
        self.camera_preview_stream = LatestFrameCamera(cap)
        self.camera_preview_engine = PoseEngine(config)
        self.camera_preview_previous_time = time.perf_counter()
        self.camera_preview_active = True
        self.camera_preview_button.setText("Zamknij podglad")
        self.status_label.setText("Podglad kamerki")
        self.preview_label.setText("")
        self.set_preview_timeline_enabled(False)
        self.preview_paused = False
        self.set_preview_pause_icon()
        self.update_preview_controls_mode()
        self.camera_preview_timer.start(max(8, int(round(1000.0 / max(config.camera_fps, 1)))))
        self.update_inline_camera_preview()

    def update_inline_camera_preview(self) -> None:
        if not self.camera_preview_active or self.camera_preview_stream is None or self.camera_preview_engine is None:
            return

        ok, frame = self.camera_preview_stream.read(timeout_seconds=0.02)
        if not ok:
            self.preview_label.setText("Brak obrazu z kamerki")
            return

        if self.mirror_check.isChecked():
            frame = cv2.flip(frame, 1)

        points, landmarks = self.camera_preview_engine.detect(frame)
        self.camera_preview_engine.draw_landmarks(frame, landmarks)
        now = time.perf_counter()
        fps = 1.0 / max(now - self.camera_preview_previous_time, 1e-6)
        self.camera_preview_previous_time = now
        draw_camera_preview_hud(frame, points, self.mirror_check.isChecked(), fps)
        self.display_preview_pixmap(self.frame_to_pixmap(frame))

    def stop_inline_camera_preview(self, restore_choreography: bool = False, restart_hand_control: bool = True) -> None:
        was_active = self.camera_preview_active
        self.camera_preview_timer.stop()
        if self.camera_preview_stream is not None:
            self.camera_preview_stream.release()
            self.camera_preview_stream = None
            self.camera_preview_cap = None
        elif self.camera_preview_cap is not None:
            self.camera_preview_cap.release()
            self.camera_preview_cap = None
        if self.camera_preview_engine is not None:
            self.camera_preview_engine.close()
            self.camera_preview_engine = None
        self.camera_preview_active = False
        self.preview_source_pixmap = None
        self.preview_paused = False
        if self.preview_fullscreen_dialog is not None:
            self.preview_fullscreen_dialog.close()
        if hasattr(self, "camera_preview_button"):
            self.camera_preview_button.setText("Podglad kamerki")
        if hasattr(self, "preview_pause_button"):
            self.set_preview_pause_icon()
        self.update_preview_controls_mode()
        if was_active and restart_hand_control:
            self.start_hand_menu_control()
        if restore_choreography:
            self.on_choreography_selected()

    def start_scan(self, video_path: Path, output_path: Path, scan_fps: float) -> None:
        if self.busy:
            return
        if not video_path.exists():
            QMessageBox.critical(self.window, "Brak filmu", "Wybierz poprawny plik wideo.")
            return
        config = self.build_camera_config()
        self.stop_hand_menu_control()
        self.stop_inline_camera_preview(restore_choreography=False, restart_hand_control=False)
        self.stop_menu_preview()
        self.run_background_task("scan", scan_video, video_path, output_path, config, scan_fps)

    def start_edit_choreography(self) -> None:
        if self.busy:
            return
        item = self.choreography_list.currentItem()
        if item is None:
            QMessageBox.warning(self.window, "Brak choreografii", "Wybierz choreografie z listy.")
            return
        choreography_path = Path(item.data(Qt.UserRole))
        if not choreography_path.exists():
            QMessageBox.critical(self.window, "Brak pliku", "Wybrany plik choreografii nie istnieje.")
            return
        self.stop_hand_menu_control()
        self.stop_inline_camera_preview(restore_choreography=False, restart_hand_control=False)
        self.stop_menu_preview()
        self.run_background_task("edit", edit_choreography, choreography_path)

    def start_play(self) -> None:
        if self.busy:
            return
        item = self.choreography_list.currentItem()
        if item is None:
            QMessageBox.warning(self.window, "Brak choreografii", "Wybierz choreografie z listy.")
            return
        choreography_path = Path(item.data(Qt.UserRole))
        if not choreography_path.exists():
            QMessageBox.critical(self.window, "Brak pliku", "Wybrany plik choreografii nie istnieje.")
            return
        config = self.build_camera_config(DEFAULT_DIFFICULTY)
        self.stop_hand_menu_control()
        self.stop_inline_camera_preview(restore_choreography=False, restart_hand_control=False)
        self.stop_menu_preview()
        self.run_background_task("play", play_choreography, choreography_path, config)

    def run_background_task(self, task_name: str, func, *args) -> None:
        self.set_busy(True, "Pracuje...")

        def target() -> None:
            try:
                result = func(*args)
                self.task_signals.finished.emit(task_name, True, "", result)
            except SystemExit as exc:
                self.task_signals.finished.emit(task_name, False, f"Przerwano. Kod: {exc.code}", None)
            except Exception as exc:
                self.task_signals.finished.emit(task_name, False, str(exc), None)

        threading.Thread(target=target, daemon=True).start()

    def on_task_finished(self, task_name: str, success: bool, message: str, result: object) -> None:
        self.set_busy(False, "")
        if not success:
            self.status_label.setText(f"Blad: {message}")
            QMessageBox.critical(self.window, "Blad", message)
            return
        if task_name == "scan":
            output_path = Path(self.scan_output_path)
            self._remember_choreography_path(output_path)
            self.refresh_choreography_library()
            self.select_choreography_path(output_path)
            self.status_label.setText(f"Skan zakonczony. Zapisano: {output_path}")
        elif task_name == "edit":
            self.refresh_choreography_library()
            self.status_label.setText("Edycja choreografii zakonczona.")
        elif task_name == "play":
            item = self.choreography_list.currentItem()
            if item is not None and isinstance(result, (int, float)):
                choreography_path = Path(item.data(Qt.UserRole))
                self.save_score_to_leaderboard(choreography_path, float(result), DEFAULT_DIFFICULTY)
                self.update_login_background()
                self.status_label.setText(
                    f"Sesja zakonczona. Wynik zapisany: {float(result):.1f}/100 ({final_grade(float(result))})."
                )
        elif task_name == "preview":
            self.status_label.setText("Podglad kamerki zamkniety.")
        if self.stack.currentWidget() in (self.login_page, self.main_page):
            self.start_hand_menu_control()

    def set_busy(self, busy: bool, status: str) -> None:
        self.busy = busy
        for button in self.busy_buttons:
            button.setEnabled(not busy)
        if status:
            self.status_label.setText(status)

    def _load_leaderboard_entries(self) -> list[dict[str, object]]:
        if not self.leaderboard_path.exists():
            return []
        try:
            payload = json.loads(self.leaderboard_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        entries = payload.get("scores", []) if isinstance(payload, dict) else payload
        return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []

    def _save_leaderboard_entries(self, entries: list[dict[str, object]]) -> None:
        self.leaderboard_path.write_text(json.dumps({"scores": entries[:100]}, indent=2), encoding="utf-8")

    def current_choreography_path(self) -> Path | None:
        if not hasattr(self, "choreography_list"):
            return None
        item = self.choreography_list.currentItem()
        if item is None:
            return None
        item_id = item.data(Qt.UserRole)
        if not item_id:
            return None
        return Path(str(item_id)).resolve()

    def refresh_leaderboard(self, choreography_path: Path | None = None) -> None:
        if choreography_path is None:
            choreography_path = self.current_choreography_path()

        entries = self._load_leaderboard_entries()
        if choreography_path is not None:
            selected_path = str(choreography_path.resolve())
            entries = [
                entry
                for entry in entries
                if str(entry.get("choreography", "")).strip()
                and str(Path(str(entry.get("choreography"))).resolve()) == selected_path
            ]
            song_title = choreography_path.stem
            try:
                data = load_choreography(choreography_path)
                source = Path(data.source_video)
                song_title = source.stem if source.name else choreography_path.stem
            except Exception:
                pass
            self.leaderboard_box.setTitle(f"Tabela wynikow: {song_title}")
        else:
            self.leaderboard_box.setTitle("Tabela wynikow")

        entries.sort(key=lambda entry: float(entry.get("score", 0.0)), reverse=True)
        self.leaderboard_table.setRowCount(0)
        for entry in entries[:20]:
            row = self.leaderboard_table.rowCount()
            self.leaderboard_table.insertRow(row)
            score = float(entry.get("score", 0.0))
            values = [
                str(entry.get("player", "")),
                str(entry.get("song", "")),
                f"{score:.1f}",
                str(entry.get("grade") or final_grade(score)),
                str(entry.get("date", "")),
            ]
            for column, value in enumerate(values):
                self.leaderboard_table.setItem(row, column, QTableWidgetItem(value))

    def save_score_to_leaderboard(self, choreography_path: Path, score: float, difficulty: str) -> None:
        self.add_points_to_current_account(score)
        song_name = choreography_path.stem
        try:
            data = load_choreography(choreography_path)
            source = Path(data.source_video)
            song_name = source.stem if source.name else choreography_path.stem
        except Exception:
            pass
        entries = self._load_leaderboard_entries()
        entries.append(
            {
                "player": self.current_player or "Gracz",
                "song": song_name,
                "score": round(score, 2),
                "grade": final_grade(score),
                "difficulty": difficulty,
                "date": time.strftime("%Y-%m-%d %H:%M"),
                "choreography": str(choreography_path.resolve()),
            }
        )
        entries.sort(key=lambda entry: float(entry.get("score", 0.0)), reverse=True)
        self._save_leaderboard_entries(entries)
        self.refresh_leaderboard(choreography_path)

    def add_points_to_current_account(self, score: float) -> None:
        if not self.current_player:
            return
        if self.current_player not in self.account_profiles:
            self.account_profiles[self.current_player] = {"name": self.current_player, "avatar": "", "xp": 0}
        gained_points = max(0, int(round(score * 10)))
        profile = self.account_profiles[self.current_player]
        profile["xp"] = int(profile.get("xp", 0) or 0) + gained_points
        self._save_account_profiles()
        self.refresh_accounts()
        self.update_player_badge()

    def close(self) -> None:
        self.login_audio_timer.stop()
        self.stop_hand_menu_control()
        self.stop_inline_camera_preview(restore_choreography=False, restart_hand_control=False)
        self.stop_menu_preview()
        if isinstance(self.login_page, VideoBackground):
            self.login_page.stop_video()
        self.window.close()
        self.app.quit()

    def run(self) -> None:
        self.window.showFullScreen()
        self.intro_page.start()
        self.app.exec()


class PoseEngine:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.pose = mp.solutions.pose.Pose(
            static_image_mode=False,
            model_complexity=self.config.pose_model_complexity,
            enable_segmentation=False,
            smooth_landmarks=True,
            min_detection_confidence=config.min_detection_confidence,
            min_tracking_confidence=config.min_tracking_confidence,
        )
        self.drawer = mp.solutions.drawing_utils
        self.pose_connections = mp.solutions.pose.POSE_CONNECTIONS

    def detect(self, frame) -> tuple[dict[str, PosePoint], object | None]:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self.pose.process(rgb)
        if not results.pose_landmarks:
            return {}, None

        points: dict[str, PosePoint] = {}
        for landmark_id in IMPORTANT_LANDMARKS:
            landmark = results.pose_landmarks.landmark[landmark_id.value]
            points[landmark_id.name] = PosePoint(
                x=float(landmark.x),
                y=float(landmark.y),
                visibility=float(landmark.visibility),
            )
        return points, results.pose_landmarks

    def draw_landmarks(self, frame, landmarks) -> None:
        if landmarks is None:
            return
        self.drawer.draw_landmarks(
            frame,
            landmarks,
            self.pose_connections,
            self.drawer.DrawingSpec(color=(0, 255, 255), thickness=2, circle_radius=3),
            self.drawer.DrawingSpec(color=(255, 128, 0), thickness=2, circle_radius=2),
        )

    def close(self) -> None:
        self.pose.close()


class AudioPlayer:
    def __init__(self, video_path: Path, prefer_python_vlc: bool = False) -> None:
        self.video_path = video_path
        self.instance = None
        self.player = None
        self.media = None
        self.process: subprocess.Popen | None = None
        self.vlc_exe_path = self._find_vlc_exe()
        self.muted = False
        self.use_process_audio = False
        self.available = False
        self.last_error: str | None = None

        if prefer_python_vlc and self._init_python_vlc():
            self.available = True
            return

        if self.vlc_exe_path is not None:
            self.use_process_audio = True
            self.available = True
            return

        if self._init_python_vlc():
            self.available = True
            return

        if vlc is None:
            self.last_error = "Brak pakietu python-vlc i nie znaleziono vlc.exe."

    def _init_python_vlc(self) -> bool:
        if vlc is None:
            return False

        try:
            self.instance = vlc.Instance("--no-video", "--quiet")
            self.player = self.instance.media_player_new()
            self.media = self.instance.media_new(str(self.video_path))
            self.player.set_media(self.media)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.player = None
            self.media = None
            self.instance = None
            return False

    @staticmethod
    def _find_vlc_exe() -> Path | None:
        path_from_env = shutil.which("vlc")
        candidates = [
            Path(path_from_env) if path_from_env else None,
            Path(r"C:\Program Files\VideoLAN\VLC\vlc.exe"),
            Path(r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"),
        ]
        for candidate in candidates:
            if candidate and candidate.exists():
                return candidate
        return None

    def play(self, start_seconds: float = 0.0) -> bool:
        if not self.available:
            return False

        start_seconds = max(0.0, float(start_seconds))
        if self.use_process_audio and self.vlc_exe_path is not None:
            self.stop()
            try:
                command = [
                    str(self.vlc_exe_path),
                    "--intf",
                    "dummy",
                    "--dummy-quiet",
                    "--no-video",
                    "--no-qt-privacy-ask",
                    "--no-volume-save",
                    "--volume",
                    "0" if self.muted else "256",
                    "--play-and-exit",
                    "--no-repeat",
                    "--no-loop",
                ]
                if start_seconds > 0.0:
                    command.extend(["--start-time", f"{start_seconds:.3f}"])
                command.append(str(self.video_path))
                self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception as exc:
                self.last_error = str(exc)
                self.process = None
                return False

        if self.player is None:
            return False

        try:
            self.player.stop()
            self.player.audio_set_mute(self.muted)
            self.player.audio_set_volume(0 if self.muted else 100)
            result = self.player.play()
            if result == -1:
                self.last_error = "VLC nie uruchomil odtwarzania."
                return False
            if start_seconds > 0.0:
                time.sleep(0.05)
                self.player.set_time(int(start_seconds * 1000))
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def stop(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            self.process = None
        if self.player is not None:
            self.player.stop()

    def is_playing(self) -> bool:
        if self.process is not None:
            return self.process.poll() is None
        if self.player is not None:
            try:
                return bool(self.player.is_playing())
            except Exception:
                return False
        return False

    def set_muted(self, muted: bool) -> bool:
        self.muted = muted
        if self.process is not None and self.process.poll() is None:
            if self.use_process_audio and self.vlc_exe_path is not None:
                self.stop()
                return self.play()
            return False
        if self.player is not None:
            try:
                self.player.audio_set_mute(muted)
                self.player.audio_set_volume(0 if muted else 100)
                return True
            except Exception as exc:
                self.last_error = str(exc)
                return False
        return True

    def status_text(self) -> str:
        if self.available:
            if self.use_process_audio and self.vlc_exe_path is not None:
                return "Audio: gotowe (VLC)"
            return "Audio: gotowe (python-vlc)"
        if self.last_error:
            return f"Audio: brak ({self.last_error})"
        return "Audio: niedostepne"


def get_windows_camera_names() -> list[str]:
    command = (
        "Get-CimInstance Win32_PnPEntity | "
        "Where-Object { $_.Name -and ("
        "$_.PNPClass -eq 'Camera' -or "
        "$_.Service -eq 'usbvideo' -or "
        "$_.Name -match '(?i)camera|webcam|web cam|cam |video|uvc'"
        ") } | "
        "Select-Object -ExpandProperty Name"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return []

    if completed.returncode != 0:
        return []

    names: list[str] = []
    for line in completed.stdout.splitlines():
        name = line.strip()
        lowered_name = name.lower()
        blocked_terms = (
            "printer",
            "drukarka",
            "scanner",
            "skaner",
            "scan",
            "fax",
            "print",
            "wsd",
            "wia",
        )
        if any(term in lowered_name for term in blocked_terms):
            continue
        if name and name not in names:
            names.append(name)
    return names


def find_available_camera_indices(max_index: int = 8) -> list[int]:
    available_indices: list[int] = []
    backend = cv2.CAP_DSHOW if hasattr(cv2, "CAP_DSHOW") else None
    for index in range(max_index + 1):
        cap = cv2.VideoCapture(index, backend) if backend is not None else cv2.VideoCapture(index)
        if cap.isOpened():
            available_indices.append(index)
        cap.release()
    return available_indices


def list_available_cameras() -> list[tuple[str, int]]:
    indices = find_available_camera_indices()
    names = get_windows_camera_names()
    if not indices:
        indices = [0]

    options: list[tuple[str, int]] = []
    for position, index in enumerate(indices):
        if position < len(names):
            label = f"{names[position]} (kamera {index})"
        else:
            label = f"Kamera {index}"
        options.append((label, index))
    return options


def open_camera(camera_index: int, width: int, height: int, fps: int = 30, prefer_mjpg: bool = True):
    backend_order = []
    if hasattr(cv2, "CAP_DSHOW"):
        backend_order.append(cv2.CAP_DSHOW)
    if hasattr(cv2, "CAP_MSMF"):
        backend_order.append(cv2.CAP_MSMF)
    backend_order.append(None)

    for backend in backend_order:
        cap = cv2.VideoCapture(camera_index, backend) if backend is not None else cv2.VideoCapture(camera_index)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if prefer_mjpg:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        if cap.isOpened():
            return cap
        cap.release()

    return None


def downscale_frame_for_performance(frame, max_width: int):
    height, width = frame.shape[:2]
    if width <= max_width:
        return frame
    scale = max_width / max(width, 1)
    return cv2.resize(
        frame,
        (max(1, int(width * scale)), max(1, int(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


class LatestFrameCamera:
    def __init__(self, cap) -> None:
        self.cap = cap
        self.lock = threading.Lock()
        self.latest_frame = None
        self.running = True
        self.failed = False
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()

    def _read_loop(self) -> None:
        while self.running:
            ok, frame = self.cap.read()
            if not ok:
                self.failed = True
                self.running = False
                break
            with self.lock:
                self.latest_frame = frame

    def read(self, timeout_seconds: float = 1.0):
        started_at = time.perf_counter()
        while self.running or self.latest_frame is not None:
            with self.lock:
                frame = self.latest_frame
            if frame is not None:
                return True, frame
            if self.failed or time.perf_counter() - started_at >= timeout_seconds:
                break
            time.sleep(0.005)
        return False, None

    def release(self) -> None:
        self.running = False
        self.thread.join(timeout=0.5)
        self.cap.release()


def get_pose_center_scale(points: dict[str, PosePoint], aspect_ratio: float = 1.0) -> tuple[tuple[float, float], float] | None:
    left_shoulder = points.get("LEFT_SHOULDER")
    right_shoulder = points.get("RIGHT_SHOULDER")
    left_hip = points.get("LEFT_HIP")
    right_hip = points.get("RIGHT_HIP")
    if not all((left_shoulder, right_shoulder, left_hip, right_hip)):
        return None

    center_x = ((left_hip.x + right_hip.x) / 2.0) * aspect_ratio
    center_y = (left_hip.y + right_hip.y) / 2.0
    shoulder_distance = math.dist(
        (left_shoulder.x * aspect_ratio, left_shoulder.y),
        (right_shoulder.x * aspect_ratio, right_shoulder.y),
    )
    torso_height = math.dist(
        (((left_shoulder.x + right_shoulder.x) / 2.0) * aspect_ratio, (left_shoulder.y + right_shoulder.y) / 2.0),
        (center_x, center_y),
    )
    scale = max(shoulder_distance, torso_height, 1e-6)
    return (center_x, center_y), scale


def measure_alignment(
    reference_points: dict[str, PosePoint],
    live_points: dict[str, PosePoint],
    reference_aspect_ratio: float = 1.0,
    live_aspect_ratio: float = 1.0,
) -> tuple[bool, str]:
    live_pose = get_pose_center_scale(live_points, live_aspect_ratio)
    if live_pose is None:
        return False, "Pokaz kamerze barki i biodra, wtedy start ruszy."

    visible_points = sum(1 for point in live_points.values() if point.visibility >= 0.5)
    if visible_points < 6:
        return False, "Pokaz kamerze troche wiecej ciala."

    reference_pose = get_pose_center_scale(reference_points, reference_aspect_ratio)
    if reference_pose is None:
        return True, "Jest okej, zaraz start."

    scoring_visible = sum(
        1
        for name, point in live_points.items()
        if name in SCORING_LANDMARK_WEIGHTS and point.visibility >= 0.45
    )
    if scoring_visible >= 5:
        return True, "Jest okej, zaraz start."

    (_, reference_scale) = reference_pose
    (_, live_scale) = live_pose
    ratio = live_scale / max(reference_scale, 1e-6)

    if ratio < 0.28:
        return False, "Podejdz troche blizej do kamery."
    if ratio > 3.2:
        return False, "Odsun sie troche od kamery."
    return True, "Jest okej, zaraz start."


def build_overlay_points(
    reference_points: dict[str, PosePoint],
    live_points: dict[str, PosePoint],
    reference_aspect_ratio: float = 1.0,
    live_aspect_ratio: float = 1.0,
) -> dict[str, tuple[float, float]]:
    normalized_live = normalize_points(live_points, live_aspect_ratio)
    reference_pose = get_pose_center_scale(reference_points, reference_aspect_ratio)
    if reference_pose is None or not normalized_live:
        return {}

    (ref_center_x, ref_center_y), ref_scale = reference_pose
    overlay_points: dict[str, tuple[float, float]] = {}
    for name, (norm_x, norm_y) in normalized_live.items():
        overlay_x = ref_center_x + norm_x * ref_scale
        overlay_points[name] = (
            overlay_x / max(reference_aspect_ratio, 1e-6),
            ref_center_y + norm_y * ref_scale,
        )
    return overlay_points


def draw_pose_from_points(
    frame,
    points: dict[str, tuple[float, float]] | dict[str, PosePoint],
    color: tuple[int, int, int],
    frame_width: int,
    frame_height: int,
    label: str | None = None,
) -> None:
    pixel_points: dict[str, tuple[int, int]] = {}
    for name, point in points.items():
        if isinstance(point, PosePoint):
            x, y = point.x, point.y
        else:
            x, y = point

        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            continue

        pixel_points[name] = (int(x * frame_width), int(y * frame_height))

    for start_name, end_name in IMPORTANT_CONNECTIONS:
        if start_name in pixel_points and end_name in pixel_points:
            cv2.line(frame, pixel_points[start_name], pixel_points[end_name], color, 3, cv2.LINE_AA)

    for name, (px, py) in pixel_points.items():
        cv2.circle(frame, (px, py), 5, color, -1, cv2.LINE_AA)

    if label and pixel_points:
        first_point = next(iter(pixel_points.values()))
        cv2.putText(
            frame,
            label,
            (first_point[0] + 10, first_point[1] - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            color,
            2,
            cv2.LINE_AA,
        )


def draw_pose_overlay(
    frame,
    points: dict[str, tuple[float, float]] | dict[str, PosePoint],
    color: tuple[int, int, int],
    frame_width: int,
    frame_height: int,
    alpha: float = 0.38,
    label: str | None = None,
) -> None:
    overlay = frame.copy()
    draw_pose_from_points(overlay, points, color, frame_width, frame_height, label=label)
    cv2.addWeighted(overlay, alpha, frame, 1.0 - alpha, 0, frame)


def draw_editable_pose(
    frame,
    points: dict[str, PosePoint],
    image_rect: tuple[int, int, int, int],
    selected_name: str | None = None,
) -> dict[str, tuple[int, int]]:
    x0, y0, image_width, image_height = image_rect
    pixel_points: dict[str, tuple[int, int]] = {}
    for name, point in points.items():
        if not (0.0 <= point.x <= 1.0 and 0.0 <= point.y <= 1.0):
            continue
        pixel_points[name] = (x0 + int(point.x * image_width), y0 + int(point.y * image_height))

    for start_name, end_name in IMPORTANT_CONNECTIONS:
        if start_name in pixel_points and end_name in pixel_points:
            cv2.line(frame, pixel_points[start_name], pixel_points[end_name], (0, 220, 255), 3, cv2.LINE_AA)

    for name, (px, py) in pixel_points.items():
        is_selected = name == selected_name
        radius = 9 if is_selected else 6
        color = (60, 255, 120) if is_selected else (255, 170, 40)
        cv2.circle(frame, (px, py), radius + 3, (0, 0, 0), -1, cv2.LINE_AA)
        cv2.circle(frame, (px, py), radius, color, -1, cv2.LINE_AA)
    return pixel_points


def get_video_frame_at(cap, timestamp: float):
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp) * 1000.0)
    ok, frame = cap.read()
    if ok:
        return frame
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    ok, frame = cap.read()
    return frame if ok else None


def build_choreography_editor_frame(
    source_frame,
    pose_frame: PoseFrame,
    frame_index: int,
    frame_count: int,
    selected_name: str | None,
    dirty: bool,
    canvas_size: tuple[int, int] = (1280, 720),
) -> tuple[object, tuple[int, int, int, int], dict[str, tuple[int, int]]]:
    canvas_width, canvas_height = canvas_size
    frame_height, frame_width = source_frame.shape[:2]
    scale = min(canvas_width / frame_width, canvas_height / frame_height)
    image_width = max(1, int(frame_width * scale))
    image_height = max(1, int(frame_height * scale))
    resized = cv2.resize(source_frame, (image_width, image_height), interpolation=cv2.INTER_AREA)
    canvas = cv2.copyMakeBorder(
        resized,
        (canvas_height - image_height) // 2,
        canvas_height - image_height - ((canvas_height - image_height) // 2),
        (canvas_width - image_width) // 2,
        canvas_width - image_width - ((canvas_width - image_width) // 2),
        cv2.BORDER_CONSTANT,
        value=(10, 14, 18),
    )
    image_rect = ((canvas_width - image_width) // 2, (canvas_height - image_height) // 2, image_width, image_height)
    pixel_points = draw_editable_pose(canvas, pose_frame.points, image_rect, selected_name)

    draw_translucent_box(canvas, (0, 0), (canvas_width, 86), (8, 13, 19), 0.72)
    status = "niezapisane zmiany" if dirty else "zapisane"
    draw_status(
        canvas,
        f"Klatka {frame_index + 1}/{frame_count}  |  {pose_frame.timestamp:.2f}s  |  {status}",
        line=0,
        color=(51, 199, 164) if not dirty else (0, 190, 255),
    )
    selected_text = selected_name if selected_name else "kliknij punkt"
    draw_status(canvas, f"Punkt: {selected_text}", line=1, color=(237, 245, 247))
    draw_translucent_box(canvas, (0, canvas_height - 58), (canvas_width, canvas_height), (8, 13, 19), 0.66)
    cv2.putText(
        canvas,
        "A/D lub strzalki: klatka   |   przeciagaj punkt myszka   |   S: zapisz   |   Q/Esc: zamknij",
        (24, canvas_height - 24),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.62,
        (237, 245, 247),
        2,
        cv2.LINE_AA,
    )
    return canvas, image_rect, pixel_points


def edit_choreography(choreography_path: Path) -> None:
    data = load_choreography(choreography_path)
    if not data.frames:
        print("Nie ma klatek do edycji w tej choreografii.")
        return

    source_video_path = Path(data.source_video)
    if not source_video_path.is_absolute():
        source_video_path = (choreography_path.parent / source_video_path).resolve()
    if not source_video_path.exists():
        print(f"Nie znaleziono filmu choreografii: {source_video_path}")
        return

    cap = cv2.VideoCapture(str(source_video_path))
    if not cap.isOpened():
        print(f"Nie udalo sie otworzyc filmu choreografii: {source_video_path}")
        return

    window_name = "Dance with me - edytor choreografii"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window_name, 1280, 720)
    state = {
        "index": 0,
        "selected": None,
        "dragging": False,
        "dirty": False,
        "image_rect": (0, 0, 1, 1),
        "pixel_points": {},
    }

    def set_point_from_mouse(x: int, y: int) -> None:
        selected = state["selected"]
        if not selected:
            return
        x0, y0, image_width, image_height = state["image_rect"]
        if image_width <= 0 or image_height <= 0:
            return
        normalized_x = max(0.0, min(1.0, (x - x0) / image_width))
        normalized_y = max(0.0, min(1.0, (y - y0) / image_height))
        pose_frame = data.frames[int(state["index"])]
        point = pose_frame.points.get(str(selected))
        if point is None:
            pose_frame.points[str(selected)] = PosePoint(normalized_x, normalized_y, 1.0)
        else:
            point.x = normalized_x
            point.y = normalized_y
            point.visibility = max(point.visibility, 0.75)
        state["dirty"] = True

    def on_mouse(event, x, y, _flags, _param) -> None:
        if event == cv2.EVENT_LBUTTONDOWN:
            pixel_points: dict[str, tuple[int, int]] = state["pixel_points"]
            closest_name = None
            closest_distance = 22.0
            for name, (px, py) in pixel_points.items():
                distance = math.dist((x, y), (px, py))
                if distance < closest_distance:
                    closest_distance = distance
                    closest_name = name
            if closest_name is not None:
                state["selected"] = closest_name
                state["dragging"] = True
                set_point_from_mouse(x, y)
        elif event == cv2.EVENT_MOUSEMOVE and state["dragging"]:
            set_point_from_mouse(x, y)
        elif event == cv2.EVENT_LBUTTONUP:
            if state["dragging"]:
                set_point_from_mouse(x, y)
            state["dragging"] = False

    cv2.setMouseCallback(window_name, on_mouse)
    try:
        while True:
            pose_frame = data.frames[int(state["index"])]
            source_frame = get_video_frame_at(cap, pose_frame.timestamp)
            if source_frame is None:
                break

            editor_frame, image_rect, pixel_points = build_choreography_editor_frame(
                source_frame,
                pose_frame,
                int(state["index"]),
                len(data.frames),
                str(state["selected"]) if state["selected"] else None,
                bool(state["dirty"]),
            )
            state["image_rect"] = image_rect
            state["pixel_points"] = pixel_points
            cv2.imshow(window_name, editor_frame)
            key = cv2.waitKey(20) & 0xFF

            if key in (ord("q"), ord("Q"), 27):
                break
            if key in (ord("s"), ord("S")):
                save_choreography(data, choreography_path)
                state["dirty"] = False
                print(f"Zapisano poprawki: {choreography_path}")
            elif key in (ord("d"), ord("D"), 83):
                state["index"] = min(len(data.frames) - 1, int(state["index"]) + 1)
                state["selected"] = None
            elif key in (ord("a"), ord("A"), 81):
                state["index"] = max(0, int(state["index"]) - 1)
                state["selected"] = None
            elif key in (ord("w"), ord("W")):
                state["index"] = min(len(data.frames) - 1, int(state["index"]) + 10)
                state["selected"] = None
            elif key in (ord("z"), ord("Z")):
                state["index"] = max(0, int(state["index"]) - 10)
                state["selected"] = None

            if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                break
    finally:
        cap.release()
        cv2.setMouseCallback(window_name, lambda *_args: None)
        try:
            cv2.destroyWindow(window_name)
        except cv2.error:
            pass


def normalize_points(points: dict[str, PosePoint], aspect_ratio: float = 1.0) -> dict[str, tuple[float, float]]:
    left_shoulder = points.get("LEFT_SHOULDER")
    right_shoulder = points.get("RIGHT_SHOULDER")
    left_hip = points.get("LEFT_HIP")
    right_hip = points.get("RIGHT_HIP")
    if not all((left_shoulder, right_shoulder, left_hip, right_hip)):
        return {}

    center_x = ((left_hip.x + right_hip.x) / 2.0) * aspect_ratio
    center_y = (left_hip.y + right_hip.y) / 2.0
    shoulder_distance = math.dist(
        (left_shoulder.x * aspect_ratio, left_shoulder.y),
        (right_shoulder.x * aspect_ratio, right_shoulder.y),
    )
    torso_height = math.dist(
        (((left_shoulder.x + right_shoulder.x) / 2.0) * aspect_ratio, (left_shoulder.y + right_shoulder.y) / 2.0),
        (center_x, center_y),
    )
    scale = max(shoulder_distance, torso_height, 1e-6)

    normalized: dict[str, tuple[float, float]] = {}
    for name, point in points.items():
        if point.visibility < 0.4:
            continue
        normalized[name] = (
            ((point.x * aspect_ratio) - center_x) / scale,
            (point.y - center_y) / scale,
        )
    return normalized


def compute_joint_angle(
    points: dict[str, tuple[float, float]],
    a: str,
    b: str,
    c: str,
) -> float | None:
    if a not in points or b not in points or c not in points:
        return None

    ax, ay = points[a]
    bx, by = points[b]
    cx, cy = points[c]
    vector_ab = (ax - bx, ay - by)
    vector_cb = (cx - bx, cy - by)

    length_ab = math.hypot(*vector_ab)
    length_cb = math.hypot(*vector_cb)
    if length_ab < 1e-6 or length_cb < 1e-6:
        return None

    cosine = ((vector_ab[0] * vector_cb[0]) + (vector_ab[1] * vector_cb[1])) / (length_ab * length_cb)
    cosine = max(-1.0, min(1.0, cosine))
    return math.degrees(math.acos(cosine))


def extract_angle_features(points: dict[str, tuple[float, float]]) -> dict[str, float]:
    features: dict[str, float] = {}
    for a, b, c in ANGLE_TRIPLETS:
        angle = compute_joint_angle(points, a, b, c)
        if angle is not None:
            features[f"{a}-{b}-{c}"] = angle
    return features


def get_difficulty_config(name: str) -> DifficultyConfig:
    return DIFFICULTY_CONFIGS.get(name, DIFFICULTY_CONFIGS[DEFAULT_DIFFICULTY])


def score_pose(
    reference_points: dict[str, PosePoint],
    live_points: dict[str, PosePoint],
    difficulty: str,
    reference_aspect_ratio: float = 1.0,
    live_aspect_ratio: float = 1.0,
) -> float | None:
    difficulty_config = get_difficulty_config(difficulty)
    normalized_reference = normalize_points(reference_points, reference_aspect_ratio)
    normalized_live = normalize_points(live_points, live_aspect_ratio)
    common_names = sorted((set(normalized_reference) & set(normalized_live)) & set(SCORING_LANDMARK_WEIGHTS))
    if len(common_names) < 4:
        return None

    weighted_distance = 0.0
    total_weight = 0.0
    for name in common_names:
        ref_x, ref_y = normalized_reference[name]
        live_x, live_y = normalized_live[name]
        point_distance = math.dist((ref_x, ref_y), (live_x, live_y))
        point_weight = SCORING_LANDMARK_WEIGHTS[name]
        weighted_distance += point_distance * point_weight
        total_weight += point_weight

    average_distance = weighted_distance / max(total_weight, 1e-6)

    reference_angles = extract_angle_features(normalized_reference)
    live_angles = extract_angle_features(normalized_live)
    common_angles = sorted(set(reference_angles) & set(live_angles))

    angle_penalty = 0.0
    if common_angles:
        total_angle_delta = 0.0
        for angle_name in common_angles:
            total_angle_delta += abs(reference_angles[angle_name] - live_angles[angle_name])
        average_angle_delta = total_angle_delta / len(common_angles)
        angle_penalty = min(60.0, average_angle_delta * difficulty_config.angle_weight)

    visibility_bonus = min(difficulty_config.visibility_bonus, max(0.0, (len(common_names) - 6) * 1.5))
    raw_score = 100.0 - (average_distance * difficulty_config.distance_weight) - angle_penalty + visibility_bonus
    return max(0.0, min(100.0, raw_score))


def estimate_pose_motion(
    previous_points: dict[str, PosePoint] | None,
    current_points: dict[str, PosePoint],
    aspect_ratio: float = 1.0,
) -> float:
    if previous_points is None:
        return 0.0
    previous_normalized = normalize_points(previous_points, aspect_ratio)
    current_normalized = normalize_points(current_points, aspect_ratio)
    distances = []
    for name in MOTION_TRACKED_LANDMARKS:
        if name in previous_normalized and name in current_normalized:
            distances.append(math.dist(previous_normalized[name], current_normalized[name]))
    if not distances:
        return 0.0
    return sum(distances) / len(distances)


def apply_motion_penalty(score: float | None, reference_motion: float, live_motion: float) -> float | None:
    if score is None:
        return None
    if reference_motion < 0.04:
        return score
    expected_motion = max(0.028, reference_motion * 0.55)
    if live_motion >= expected_motion:
        return score
    missing_ratio = 1.0 - (live_motion / expected_motion)
    penalty = min(82.0, 22.0 + (missing_ratio * 68.0))
    return max(0.0, score - penalty)


def cap_score_for_missing_motion(score: float | None, reference_motion: float, live_motion: float) -> float | None:
    if score is None:
        return None
    if reference_motion < 0.18:
        return score

    motion_ratio = live_motion / max(reference_motion, 1e-6)
    if live_motion < 0.035 or motion_ratio < 0.18:
        return min(score, 18.0)
    if motion_ratio < 0.32:
        return min(score, 34.0)
    if motion_ratio < 0.48:
        return min(score, 52.0)
    return score


def update_accuracy_indicator(accuracy: float, score: float | None, delta_seconds: float, difficulty: str) -> float:
    difficulty_config = get_difficulty_config(difficulty)
    delta_seconds = max(delta_seconds, 0.0)
    if score is None:
        loss = 32.0 * delta_seconds
        return max(0.0, min(100.0, accuracy - loss))

    if score >= difficulty_config.perfect_threshold:
        gain = (100.0 - accuracy) * 0.22 * delta_seconds
        gain += 2.3 * delta_seconds
        return max(0.0, min(100.0, accuracy + gain))

    if score >= difficulty_config.good_threshold:
        gain = (100.0 - accuracy) * 0.08 * delta_seconds
        gain += 0.7 * delta_seconds
        return max(0.0, min(100.0, accuracy + gain))

    if score >= difficulty_config.ok_threshold:
        loss = (difficulty_config.good_threshold - score) * 0.035 * delta_seconds
        return max(0.0, min(100.0, accuracy - loss))

    loss = (difficulty_config.ok_threshold - score) * 0.42 * delta_seconds
    loss += 7.0 * delta_seconds
    return max(0.0, min(100.0, accuracy - loss))


def score_segment_for_accuracy(score: float | None, difficulty: str) -> float | None:
    if score is None:
        return None

    difficulty_config = get_difficulty_config(difficulty)
    if score >= difficulty_config.perfect_threshold:
        return min(100.0, score + ((100.0 - score) * 0.75) + 4.0)
    if score >= difficulty_config.good_threshold:
        return min(100.0, score + ((100.0 - score) * 0.38) + 1.5)
    if score < difficulty_config.ok_threshold:
        return max(0.0, score * 0.32 - 16.0)
    return score


def final_grade(score: float) -> str:
    if score >= 90.0:
        return "S"
    if score >= 85.0:
        return "A"
    if score >= 78.0:
        return "B"
    if score >= 68.0:
        return "C"
    return "D"


def save_choreography(data: ChoreographyData, output_path: Path) -> None:
    payload = asdict(data)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_choreography(path: Path) -> ChoreographyData:
    payload = json.loads(path.read_text(encoding="utf-8"))
    frames = [
        PoseFrame(
            timestamp=frame["timestamp"],
            points={name: PosePoint(**point) for name, point in frame["points"].items()},
        )
        for frame in payload["frames"]
    ]
    return ChoreographyData(
        source_video=payload["source_video"],
        fps=payload["fps"],
        duration_seconds=payload["duration_seconds"],
        frame_count=payload["frame_count"],
        frames=frames,
    )


def find_reference_frame(data: ChoreographyData, elapsed_seconds: float) -> PoseFrame:
    if not data.frames:
        raise ValueError("Brak zeskanowanych klatek choreografii.")

    clamped_time = min(max(elapsed_seconds, 0.0), data.duration_seconds)
    best_frame = data.frames[-1]
    for frame in data.frames:
        if frame.timestamp >= clamped_time:
            best_frame = frame
            break
    return best_frame


def find_reference_candidates(data: ChoreographyData, elapsed_seconds: float, window_seconds: float) -> list[PoseFrame]:
    if not data.frames:
        return []

    start_time = max(0.0, elapsed_seconds - window_seconds)
    end_time = min(data.duration_seconds, elapsed_seconds + window_seconds)
    candidates = [frame for frame in data.frames if start_time <= frame.timestamp <= end_time]
    if candidates:
        return candidates
    return [find_reference_frame(data, elapsed_seconds)]


def score_pose_with_timing_window(
    data: ChoreographyData,
    elapsed_seconds: float,
    live_points: dict[str, PosePoint],
    difficulty: str,
    reference_aspect_ratio: float = 1.0,
    live_aspect_ratio: float = 1.0,
) -> tuple[PoseFrame, float | None, float]:
    difficulty_config = get_difficulty_config(difficulty)
    candidates = find_reference_candidates(data, elapsed_seconds, difficulty_config.timing_window_seconds)

    best_frame = candidates[0]
    best_score: float | None = None
    best_offset = best_frame.timestamp - elapsed_seconds

    for candidate in candidates:
        candidate_score = score_pose(
            candidate.points,
            live_points,
            difficulty,
            reference_aspect_ratio,
            live_aspect_ratio,
        )
        if candidate_score is None:
            continue
        if best_score is None or candidate_score > best_score:
            best_frame = candidate
            best_score = candidate_score
            best_offset = candidate.timestamp - elapsed_seconds

    return best_frame, best_score, best_offset


def draw_status(frame, text: str, line: int, color: tuple[int, int, int] = (255, 255, 255)) -> None:
    x = 20
    y = 35 + line * 30
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2, cv2.LINE_AA)


def draw_translucent_box(
    frame,
    top_left: tuple[int, int],
    bottom_right: tuple[int, int],
    color: tuple[int, int, int],
    alpha: float,
) -> None:
    overlay = frame.copy()
    cv2.rectangle(overlay, top_left, bottom_right, color, -1)
    cv2.addWeighted(overlay, alpha, frame, 1.0 - alpha, 0, frame)


def draw_camera_preview_hud(frame, points: dict[str, PosePoint], mirror_view: bool, fps: float) -> None:
    height, width = frame.shape[:2]
    draw_translucent_box(frame, (0, 0), (width, 78), (8, 13, 19), 0.68)
    draw_translucent_box(frame, (18, height - 72), (width - 18, height - 18), (8, 13, 19), 0.58)

    cv2.putText(
        frame,
        "Dance with me - podglad kamerki",
        (28, 32),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.78,
        (0, 0, 0),
        4,
        cv2.LINE_AA,
    )
    cv2.putText(
        frame,
        "Dance with me - podglad kamerki",
        (28, 32),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.78,
        (237, 245, 247),
        2,
        cv2.LINE_AA,
    )

    status_text = "MediaPipe: wykryto poze" if points else "MediaPipe: ustaw sie w kadrze"
    status_color = (51, 199, 164) if points else (0, 190, 255)
    mirror_text = "Lustro: wlaczone" if mirror_view else "Lustro: wylaczone"
    info = f"{status_text}   |   {mirror_text}   |   FPS: {fps:.0f}"
    cv2.putText(frame, info, (28, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.54, status_color, 2, cv2.LINE_AA)

    close_text = "Zamknij: X albo Q"
    text_size, _baseline = cv2.getTextSize(close_text, cv2.FONT_HERSHEY_SIMPLEX, 0.56, 2)
    cv2.putText(
        frame,
        close_text,
        (width - text_size[0] - 28, 50),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.56,
        (190, 205, 214),
        2,
        cv2.LINE_AA,
    )

    bottom_text = "Sprawdz, czy szkielet jest stabilny i czy odbicie lustrzane zgadza sie z Twoim ruchem."
    cv2.putText(frame, bottom_text, (34, height - 38), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (237, 245, 247), 2, cv2.LINE_AA)


def draw_centered_text(
    frame,
    text: str,
    center: tuple[int, int],
    font_scale: float,
    color: tuple[int, int, int],
    thickness: int = 2,
) -> None:
    text_size, _baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
    x = int(center[0] - text_size[0] / 2)
    y = int(center[1] + text_size[1] / 2)
    cv2.putText(frame, text, (x + 2, y + 2), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, thickness, cv2.LINE_AA)


def draw_verdict_popup(
    frame,
    text: str,
    color: tuple[int, int, int],
    score: float | None,
    elapsed_seconds: float,
    duration_seconds: float = 1.35,
) -> None:
    progress = max(0.0, min(1.0, elapsed_seconds / duration_seconds))
    if progress >= 1.0:
        return

    height, width = frame.shape[:2]
    scale = max(0.75, min(1.55, min(width, height) / 720.0))
    alpha = math.sin(progress * math.pi)
    slide = math.sin(progress * math.pi)
    x = int(26 * scale)
    hidden_y = height + int(42 * scale)
    visible_y = height - int(42 * scale)
    y = int(hidden_y - ((hidden_y - visible_y) * slide))

    label = f"{text.upper()}"
    text_alpha_color = tuple(int(channel * alpha + 255 * (1.0 - alpha)) for channel in color)
    cv2.putText(
        frame,
        label,
        (x + int(2 * scale), y + int(2 * scale)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.95 * scale,
        (0, 0, 0),
        max(3, int(5 * scale)),
        cv2.LINE_AA,
    )
    cv2.putText(
        frame,
        label,
        (x, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.95 * scale,
        text_alpha_color,
        max(2, int(3 * scale)),
        cv2.LINE_AA,
    )


def draw_game_hud(frame, accuracy_percent: float, elapsed_seconds: float, duration_seconds: float) -> None:
    height, width = frame.shape[:2]
    scale = max(0.78, min(1.55, min(width, height) / 720.0))
    progress = max(0.0, min(1.0, elapsed_seconds / max(duration_seconds, 1e-6)))
    margin = int(26 * scale)
    radius = int(24 * scale)
    thickness = max(2, int(3 * scale))
    center = (width - margin - radius, margin + radius)

    cv2.circle(frame, center, radius, (18, 25, 31), -1, cv2.LINE_AA)
    cv2.circle(frame, center, radius, (90, 110, 120), thickness, cv2.LINE_AA)
    if progress > 0.0:
        axes = (radius - thickness, radius - thickness)
        cv2.ellipse(frame, center, axes, -90, 0, 360.0 * progress, (51, 199, 164), -1, cv2.LINE_AA)
        cv2.circle(frame, center, max(1, int(radius * 0.55)), (18, 25, 31), -1, cv2.LINE_AA)
    cv2.circle(frame, center, radius, (237, 245, 247), thickness, cv2.LINE_AA)

    percent_text = f"{accuracy_percent:.1f}%"
    font_scale = 0.88 * scale
    text_thickness = max(2, int(2 * scale))
    text_size, _baseline = cv2.getTextSize(percent_text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, text_thickness)
    text_x = center[0] - radius - int(18 * scale) - text_size[0]
    text_y = center[1] + text_size[1] // 2
    cv2.putText(
        frame,
        percent_text,
        (text_x + int(2 * scale), text_y + int(2 * scale)),
        cv2.FONT_HERSHEY_SIMPLEX,
        font_scale,
        (0, 0, 0),
        text_thickness + 2,
        cv2.LINE_AA,
    )
    cv2.putText(
        frame,
        percent_text,
        (text_x, text_y),
        cv2.FONT_HERSHEY_SIMPLEX,
        font_scale,
        (237, 245, 247),
        text_thickness,
        cv2.LINE_AA,
    )


def draw_final_result_frame(frame, score: float, elapsed_seconds: float) -> None:
    height, width = frame.shape[:2]
    grade = final_grade(score)
    scale = max(0.82, min(1.7, min(width, height) / 720.0))
    grade_settings = {
        "S": ((51, 230, 170), 1.0, "FENOMENALNIE"),
        "A": ((70, 210, 255), 0.82, "SWIETNIE"),
        "B": ((80, 170, 255), 0.62, "BARDZO DOBRZE"),
        "C": ((0, 190, 255), 0.42, "DOBRZE"),
        "D": ((80, 90, 255), 0.22, "JESZCZE RAZ"),
    }
    color, energy, title = grade_settings[grade]

    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (width, height), (8, 12, 18), -1)
    cv2.addWeighted(overlay, 0.64, frame, 0.36, 0, frame)

    pulse = 1.0 + (0.055 * energy * math.sin(elapsed_seconds * 5.5))
    center = (width // 2, height // 2)
    grade_scale = 4.2 * scale * pulse
    grade_thickness = max(5, int(9 * scale))
    grade_size, _baseline = cv2.getTextSize(grade, cv2.FONT_HERSHEY_SIMPLEX, grade_scale, grade_thickness)
    grade_x = int(center[0] - grade_size[0] / 2)
    grade_y = int(center[1] + grade_size[1] / 2 - 42 * scale)

    ring_radius = int((120 + 24 * math.sin(elapsed_seconds * 3.0)) * scale * (0.75 + energy * 0.25))
    cv2.circle(frame, (center[0], int(center[1] - 48 * scale)), ring_radius, color, max(2, int(3 * scale)), cv2.LINE_AA)
    cv2.putText(frame, grade, (grade_x + int(5 * scale), grade_y + int(5 * scale)), cv2.FONT_HERSHEY_SIMPLEX, grade_scale, (0, 0, 0), grade_thickness + 4, cv2.LINE_AA)
    cv2.putText(frame, grade, (grade_x, grade_y), cv2.FONT_HERSHEY_SIMPLEX, grade_scale, color, grade_thickness, cv2.LINE_AA)

    title_scale = 0.92 * scale
    title_thickness = max(2, int(2 * scale))
    title_size, _baseline = cv2.getTextSize(title, cv2.FONT_HERSHEY_SIMPLEX, title_scale, title_thickness)
    title_pos = (int(center[0] - title_size[0] / 2), int(center[1] + 112 * scale))
    cv2.putText(frame, title, (title_pos[0] + 2, title_pos[1] + 2), cv2.FONT_HERSHEY_SIMPLEX, title_scale, (0, 0, 0), title_thickness + 2, cv2.LINE_AA)
    cv2.putText(frame, title, title_pos, cv2.FONT_HERSHEY_SIMPLEX, title_scale, (237, 245, 247), title_thickness, cv2.LINE_AA)

    score_text = f"{score:.1f}%"
    score_scale = 0.78 * scale
    score_size, _baseline = cv2.getTextSize(score_text, cv2.FONT_HERSHEY_SIMPLEX, score_scale, title_thickness)
    score_pos = (int(center[0] - score_size[0] / 2), int(center[1] + 158 * scale))
    cv2.putText(frame, score_text, (score_pos[0] + 2, score_pos[1] + 2), cv2.FONT_HERSHEY_SIMPLEX, score_scale, (0, 0, 0), title_thickness + 2, cv2.LINE_AA)
    cv2.putText(frame, score_text, score_pos, cv2.FONT_HERSHEY_SIMPLEX, score_scale, color, title_thickness, cv2.LINE_AA)

    particle_count = int(10 + energy * 34)
    for index in range(particle_count):
        phase = elapsed_seconds * (0.55 + energy) + index * 0.73
        orbit = (120 + (index % 7) * 24) * scale
        px = int(center[0] + math.cos(phase) * orbit)
        py = int(center[1] - 42 * scale + math.sin(phase * 1.37) * orbit * 0.55)
        if 0 <= px < width and 0 <= py < height:
            radius = max(2, int((2 + (index % 4) + energy * 3) * scale))
            particle_color = color if index % 3 else (237, 245, 247)
            cv2.circle(frame, (px, py), radius, particle_color, -1, cv2.LINE_AA)


def show_final_result_animation(window_name: str, frame, score: float, seconds: float = 4.2) -> None:
    started_at = time.perf_counter()
    while True:
        elapsed = time.perf_counter() - started_at
        result_frame = frame.copy()
        draw_final_result_frame(result_frame, score, elapsed)
        cv2.imshow(window_name, result_frame)
        key = cv2.waitKey(20) & 0xFF
        if elapsed >= seconds or key in (ord("q"), ord("Q"), 27, 13, 32):
            break
        if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
            break


def frame_for_window(window_name: str, frame, background: tuple[int, int, int] = (0, 0, 0)):
    try:
        _x, _y, window_width, window_height = cv2.getWindowImageRect(window_name)
    except cv2.error:
        return frame

    if window_width <= 0 or window_height <= 0:
        return frame

    frame_height, frame_width = frame.shape[:2]
    scale = min(window_width / frame_width, window_height / frame_height)
    display_width = max(1, int(frame_width * scale))
    display_height = max(1, int(frame_height * scale))
    display_frame = (
        cv2.resize(frame, (display_width, display_height), interpolation=cv2.INTER_AREA)
        if scale < 1.0
        else frame
    )

    canvas = cv2.copyMakeBorder(
        display_frame,
        (window_height - display_height) // 2,
        window_height - display_height - ((window_height - display_height) // 2),
        (window_width - display_width) // 2,
        window_width - display_width - ((window_width - display_width) // 2),
        cv2.BORDER_CONSTANT,
        value=background,
    )
    return canvas


def window_canvas_size(window_name: str, fallback_frame) -> tuple[int, int]:
    try:
        _x, _y, window_width, window_height = cv2.getWindowImageRect(window_name)
    except cv2.error:
        fallback_height, fallback_width = fallback_frame.shape[:2]
        return fallback_width, fallback_height
    if window_width <= 0 or window_height <= 0:
        fallback_height, fallback_width = fallback_frame.shape[:2]
        return fallback_width, fallback_height
    return window_width, window_height


def frame_on_canvas(frame, canvas_width: int, canvas_height: int, background: tuple[int, int, int] = (0, 0, 0)):
    frame_height, frame_width = frame.shape[:2]
    scale = min(canvas_width / frame_width, canvas_height / frame_height)
    display_width = max(1, int(frame_width * scale))
    display_height = max(1, int(frame_height * scale))
    display_frame = cv2.resize(frame, (display_width, display_height), interpolation=cv2.INTER_AREA)
    return cv2.copyMakeBorder(
        display_frame,
        (canvas_height - display_height) // 2,
        canvas_height - display_height - ((canvas_height - display_height) // 2),
        (canvas_width - display_width) // 2,
        canvas_width - display_width - ((canvas_width - display_width) // 2),
        cv2.BORDER_CONSTANT,
        value=background,
    )


def build_pause_frame(frame) -> tuple[object, dict[str, tuple[int, int, int, int]]]:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    pause_frame = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    draw_translucent_box(pause_frame, (0, 0), (pause_frame.shape[1], pause_frame.shape[0]), (35, 35, 35), 0.46)

    height, width = pause_frame.shape[:2]
    draw_centered_text(pause_frame, "PAUZA", (width // 2, int(height * 0.26)), 1.45, (237, 245, 247), 3)

    tile_width = min(420, max(280, int(width * 0.36)))
    tile_height = min(112, max(86, int(height * 0.13)))
    gap = 18
    start_x = int((width - tile_width) / 2)
    start_y = int(height * 0.38)
    tiles = [
        ("resume", "WZNOW", (48, 196, 132)),
        ("restart", "POWTORZ", (245, 151, 54)),
        ("menu", "MENU", (220, 70, 78)),
    ]
    buttons: dict[str, tuple[int, int, int, int]] = {}
    for index, (action, label, color) in enumerate(tiles):
        x = start_x
        y = start_y + index * (tile_height + gap)
        buttons[action] = (x, y, x + tile_width, y + tile_height)
        draw_translucent_box(pause_frame, (x + 8, y + 10), (x + tile_width + 8, y + tile_height + 10), (0, 0, 0), 0.28)
        cv2.rectangle(pause_frame, (x, y), (x + tile_width, y + tile_height), color, -1)
        cv2.rectangle(pause_frame, (x, y), (x + tile_width, y + tile_height), (255, 255, 255), 2)
        draw_centered_text(pause_frame, label, (x + tile_width // 2, y + tile_height // 2), 0.9, (255, 255, 255), 2)
    return pause_frame, buttons


def build_game_over_frame(frame, accuracy: float) -> tuple[object, dict[str, tuple[int, int, int, int]]]:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    game_over_frame = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    draw_translucent_box(game_over_frame, (0, 0), (game_over_frame.shape[1], game_over_frame.shape[0]), (25, 18, 22), 0.58)

    height, width = game_over_frame.shape[:2]
    draw_centered_text(game_over_frame, "GAME OVER", (width // 2, int(height * 0.25)), 1.55, (70, 70, 255), 4)
    draw_centered_text(
        game_over_frame,
        f"Dokladnosc spadla do {accuracy:.0f}%",
        (width // 2, int(height * 0.34)),
        0.78,
        (237, 245, 247),
        2,
    )

    tile_width = min(430, max(280, int(width * 0.38)))
    tile_height = min(112, max(86, int(height * 0.13)))
    gap = 20
    start_x = int((width - tile_width) / 2)
    start_y = int(height * 0.46)
    tiles = [
        ("restart", "POWTORZ", (245, 151, 54)),
        ("menu", "MENU", (220, 70, 78)),
    ]
    buttons: dict[str, tuple[int, int, int, int]] = {}
    for index, (action, label, color) in enumerate(tiles):
        x = start_x
        y = start_y + index * (tile_height + gap)
        buttons[action] = (x, y, x + tile_width, y + tile_height)
        draw_translucent_box(game_over_frame, (x + 8, y + 10), (x + tile_width + 8, y + tile_height + 10), (0, 0, 0), 0.3)
        cv2.rectangle(game_over_frame, (x, y), (x + tile_width, y + tile_height), color, -1)
        cv2.rectangle(game_over_frame, (x, y), (x + tile_width, y + tile_height), (255, 255, 255), 2)
        draw_centered_text(game_over_frame, label, (x + tile_width // 2, y + tile_height // 2), 0.9, (255, 255, 255), 2)
    return game_over_frame, buttons


def wait_for_game_over_action(window_name: str, frame, accuracy: float) -> str:
    window_width, window_height = window_canvas_size(window_name, frame)
    displayed_frame = frame_on_canvas(frame, window_width, window_height, background=(35, 30, 32))
    game_over_frame, buttons = build_game_over_frame(displayed_frame, accuracy)
    selected_action = {"value": ""}

    def on_mouse(event, x, y, _flags, _param) -> None:
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        for action, (x1, y1, x2, y2) in buttons.items():
            if x1 <= x <= x2 and y1 <= y <= y2:
                selected_action["value"] = action
                return

    cv2.setMouseCallback(window_name, on_mouse)
    try:
        while not selected_action["value"]:
            if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                selected_action["value"] = "menu"
                break
            cv2.imshow(window_name, game_over_frame)
            key = cv2.waitKey(20) & 0xFF
            if key in (ord("1"), ord("r"), ord("R"), 13, 32):
                selected_action["value"] = "restart"
            elif key in (ord("2"), ord("m"), ord("M"), ord("q"), ord("Q"), 27):
                selected_action["value"] = "menu"
    finally:
        cv2.setMouseCallback(window_name, lambda *_args: None)
    return selected_action["value"]


def wait_for_pause_action(window_name: str, frame) -> str:
    window_width, window_height = window_canvas_size(window_name, frame)
    displayed_frame = frame_on_canvas(frame, window_width, window_height, background=(45, 45, 45))
    pause_frame, buttons = build_pause_frame(displayed_frame)
    selected_action = {"value": ""}

    def on_mouse(event, x, y, _flags, _param) -> None:
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        for action, (x1, y1, x2, y2) in buttons.items():
            if x1 <= x <= x2 and y1 <= y <= y2:
                selected_action["value"] = action
                return

    cv2.setMouseCallback(window_name, on_mouse)
    try:
        while not selected_action["value"]:
            if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                selected_action["value"] = "menu"
                break
            cv2.imshow(window_name, pause_frame)
            key = cv2.waitKey(20) & 0xFF
            if key in (ord("1"), ord("r"), ord("R"), 13, 32):
                selected_action["value"] = "resume"
            elif key in (ord("2"), ord("p"), ord("P")):
                selected_action["value"] = "restart"
            elif key in (ord("3"), ord("m"), ord("M"), ord("q"), ord("Q")):
                selected_action["value"] = "menu"
            elif key == 27:
                selected_action["value"] = "resume"
    finally:
        cv2.setMouseCallback(window_name, lambda *_args: None)
    return selected_action["value"]


def show_resume_countdown(window_name: str, frame, seconds: int = 4) -> bool:
    window_width, window_height = window_canvas_size(window_name, frame)
    displayed_frame = frame_on_canvas(frame, window_width, window_height, background=(45, 45, 45))
    started_at = time.perf_counter()
    while True:
        remaining = seconds - int(time.perf_counter() - started_at)
        if remaining <= 0:
            return True
        countdown_frame, _buttons = build_pause_frame(displayed_frame)
        draw_centered_text(
            countdown_frame,
            f"START ZA {remaining}",
            (countdown_frame.shape[1] // 2, int(countdown_frame.shape[0] * 0.32)),
            1.25,
            (51, 199, 164),
            3,
        )
        cv2.imshow(window_name, countdown_frame)
        key = cv2.waitKey(30) & 0xFF
        if key in (ord("q"), ord("Q")) or cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
            return False


def preview_camera(config: AppConfig) -> None:
    engine = PoseEngine(config)
    cap = open_camera(config.camera_index, config.width, config.height, config.camera_fps, config.prefer_mjpg)
    if cap is None:
        print(
            "Nie udalo sie otworzyc kamerki. Sprawdz, czy nie jest zajeta przez inna aplikacje "
            "albo sprobuj innego numeru kamerki, np. 0, 1 lub 2."
        )
        engine.close()
        sys.exit(1)

    window_name = "Dance with me - camera preview"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window_name, 960, 720)
    live_camera = LatestFrameCamera(cap)
    previous_time = time.perf_counter()

    try:
        while True:
            if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                break

            ok, frame = live_camera.read()
            if not ok:
                print("Nie udalo sie odczytac klatki z kamerki.")
                break

            if config.mirror_view:
                frame = cv2.flip(frame, 1)

            points, landmarks = engine.detect(frame)
            engine.draw_landmarks(frame, landmarks)
            now = time.perf_counter()
            fps = 1.0 / max(now - previous_time, 1e-6)
            previous_time = now
            draw_camera_preview_hud(frame, points, config.mirror_view, fps)
            cv2.imshow(window_name, frame)
            if cv2.waitKey(1) & 0xFF in (ord("q"), ord("Q")):
                break
    finally:
        live_camera.release()
        engine.close()
        try:
            cv2.destroyWindow(window_name)
        except cv2.error:
            pass


def scan_video(video_path: Path, output_path: Path, config: AppConfig, scan_fps: float) -> None:
    engine = PoseEngine(config)
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        print(f"Nie udalo sie otworzyc pliku wideo: {video_path}")
        sys.exit(1)

    video_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration_seconds = total_frames / video_fps if total_frames and video_fps else 0.0
    frame_step = max(1, round(video_fps / scan_fps))
    frames: list[PoseFrame] = []

    # print(f"Skanowanie filmu: {video_path.name}")
    # print(f"FPS filmu: {video_fps:.2f}, skan co {frame_step} klatke/klatki")

    frame_index = 0
    scanned_index = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break

            if frame_index % frame_step == 0:
                scan_frame = downscale_frame_for_performance(frame, GAME_MAX_FRAME_WIDTH)
                points, landmarks = engine.detect(scan_frame)
                timestamp = frame_index / video_fps if video_fps else 0.0
                if points:
                    frames.append(PoseFrame(timestamp=timestamp, points=points))
                scanned_index += 1

                preview = scan_frame.copy()
                engine.draw_landmarks(preview, landmarks)
                draw_status(preview, f"Skan: {scanned_index}", line=0)
                draw_status(preview, f"Zapisane klatki: {len(frames)}", line=1)
                draw_status(preview, "Q aby przerwac skanowanie", line=2)
                cv2.imshow("Dance with me - scan", preview)
                if cv2.waitKey(1) & 0xFF in (ord("q"), ord("Q")):
                    break

            frame_index += 1
    finally:
        cap.release()
        cv2.destroyAllWindows()
        engine.close()

    data = ChoreographyData(
        source_video=str(video_path),
        fps=scan_fps,
        duration_seconds=duration_seconds,
        frame_count=len(frames),
        frames=frames,
    )
    save_choreography(data, output_path)
    print(f"Zapisano choreografie do: {output_path}")
    print(f"Liczba zapisanych klatek z pozycja: {len(frames)}")


def play_choreography(choreography_path: Path, config: AppConfig) -> float | None:
    data = load_choreography(choreography_path)
    if not data.frames:
        print("Plik choreografii nie zawiera zadnych wykrytych poz.")
        sys.exit(1)

    source_video_path = Path(data.source_video)
    if not source_video_path.is_absolute():
        source_video_path = (choreography_path.parent / source_video_path).resolve()
    if not source_video_path.exists():
        print(f"Nie znaleziono oryginalnego filmu choreografii: {source_video_path}")
        sys.exit(1)

    video_cap = cv2.VideoCapture(str(source_video_path))
    if not video_cap.isOpened():
        print(f"Nie udalo sie otworzyc filmu choreografii: {source_video_path}")
        sys.exit(1)

    ok, first_video_frame = video_cap.read()
    if not ok:
        print("Nie udalo sie odczytac pierwszej klatki filmu choreografii.")
        video_cap.release()
        sys.exit(1)
    first_video_frame = downscale_frame_for_performance(first_video_frame, GAME_MAX_FRAME_WIDTH)

    video_fps = video_cap.get(cv2.CAP_PROP_FPS) or 30.0
    video_total_frames = int(video_cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    video_height, video_width = first_video_frame.shape[:2]
    reference_aspect_ratio = video_width / max(video_height, 1)
    audio_player = AudioPlayer(source_video_path)
    audio_enabled = False
    if not audio_player.available and audio_player.last_error:
        print(f"Audio niedostepne: {audio_player.last_error}")

    engine = PoseEngine(config)
    cap = open_camera(config.camera_index, config.width, config.height, config.camera_fps, config.prefer_mjpg)
    if cap is None:
        print(
            "Nie udalo sie otworzyc kamerki. Sprawdz, czy nie jest zajeta przez inna aplikacje "
            "albo sprobuj innego numeru kamerki, np. 0, 1 lub 2."
        )
        video_cap.release()
        sys.exit(1)
    camera_width = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or config.width
    camera_height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or config.height
    live_aspect_ratio = camera_width / max(camera_height, 1)
    live_camera = LatestFrameCamera(cap)

    waiting_reference = data.frames[0]
    play_started_at: float | None = None
    alignment_started_at: float | None = None
    countdown_seconds = 5
    accuracy_percent = 100.0
    score_total = 0.0
    score_count = 0
    previous_live_points: dict[str, PosePoint] | None = None
    previous_reference_points: dict[str, PosePoint] | None = None
    previous_time = time.perf_counter()
    recent_scores: deque[tuple[float, float | None]] = deque()
    recent_live_motion = 0.0
    recent_reference_motion = 0.0
    last_verdict_at = 2.0
    verdict_popup: tuple[str, tuple[int, int, int], float | None, float] | None = None
    previous_video_frame_index = 0
    last_video_frame = first_video_frame.copy()
    print("Tryb gry uruchomiony. Najpierw ustaw sie do wzorca, potem zacznie sie odliczanie.")
    # print(f"Poziom trudnosci: {config.difficulty}")

    window_name = "Dance with me"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    is_fullscreen = config.fullscreen
    if is_fullscreen:
        cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    try:
        while True:
            if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                return None

            ok, camera_frame = live_camera.read()
            if not ok:
                print("Nie udalo sie odczytac klatki z kamerki.")
                break

            if config.mirror_view:
                camera_frame = cv2.flip(camera_frame, 1)

            live_points, _ = engine.detect(camera_frame)
            now = time.perf_counter()
            fps = 1.0 / max(now - previous_time, 1e-6)
            previous_time = now

            if play_started_at is None:
                display_frame = first_video_frame.copy()
                frame_height, frame_width = display_frame.shape[:2]
                overlay_points = build_overlay_points(
                    waiting_reference.points,
                    live_points,
                    reference_aspect_ratio,
                    live_aspect_ratio,
                )
                draw_pose_overlay(
                    display_frame,
                    overlay_points,
                    (0, 255, 255),
                    frame_width,
                    frame_height,
                    alpha=0.35,
                    label="Ty",
                )

                aligned, alignment_message = measure_alignment(
                    waiting_reference.points,
                    live_points,
                    reference_aspect_ratio,
                    live_aspect_ratio,
                )
                if aligned:
                    if alignment_started_at is None:
                        alignment_started_at = now
                    countdown_left = max(0, math.ceil(countdown_seconds - (now - alignment_started_at)))
                    if now - alignment_started_at >= countdown_seconds:
                        play_started_at = time.perf_counter()
                        video_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        previous_video_frame_index = 0
                        last_video_frame = first_video_frame.copy()
                        accuracy_percent = 100.0
                        score_total = 0.0
                        score_count = 0
                        previous_live_points = None
                        previous_reference_points = None
                        recent_scores.clear()
                        recent_live_motion = 0.0
                        recent_reference_motion = 0.0
                        last_verdict_at = 2.0
                        verdict_popup = None
                        audio_enabled = audio_player.play()
                        if not audio_enabled:
                            print(f"Audio nie wystartowalo: {audio_player.status_text()}")
                        continue
                    draw_status(display_frame, f"Start za: {countdown_left}", line=0, color=(0, 220, 0))
                    draw_status(display_frame, alignment_message, line=1, color=(0, 220, 0))
                else:
                    alignment_started_at = None
                    draw_status(display_frame, "Dopasuj swoja odleglosc i poze do wzorca", line=0, color=(0, 165, 255))
                    draw_status(display_frame, alignment_message, line=1, color=(0, 165, 255))

                # draw_status(display_frame, "Twoj szkielet jest nakladany na film referencyjny", line=2)
                # draw_status(display_frame, "Gdy dopasowanie bedzie dobre, ruszy odliczanie", line=3)
                # draw_status(display_frame, "Q aby wyjsc", line=5)
            else:
                elapsed_seconds = time.perf_counter() - play_started_at
                target_frame_index = int(elapsed_seconds * video_fps)
                if video_total_frames:
                    target_frame_index = min(target_frame_index, max(video_total_frames - 1, 0))

                if target_frame_index == previous_video_frame_index:
                    display_frame = last_video_frame.copy()
                else:
                    if target_frame_index != previous_video_frame_index + 1:
                        video_cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame_index)
                    ok, display_frame = video_cap.read()
                    if not ok:
                        break
                    display_frame = downscale_frame_for_performance(display_frame, GAME_MAX_FRAME_WIDTH)
                    previous_video_frame_index = target_frame_index
                    last_video_frame = display_frame.copy()

                frame_height, frame_width = display_frame.shape[:2]
                display_reference_frame = find_reference_frame(data, elapsed_seconds)
                score_reference_frame, current_score, timing_offset = score_pose_with_timing_window(
                    data,
                    elapsed_seconds,
                    live_points,
                    config.difficulty,
                    reference_aspect_ratio,
                    live_aspect_ratio,
                )
                live_motion = estimate_pose_motion(previous_live_points, live_points, live_aspect_ratio)
                reference_motion = estimate_pose_motion(
                    previous_reference_points,
                    score_reference_frame.points,
                    reference_aspect_ratio,
                )
                current_score = apply_motion_penalty(current_score, reference_motion, live_motion)
                previous_live_points = live_points
                previous_reference_points = score_reference_frame.points

                scoring_active = elapsed_seconds >= 2.0
                if scoring_active:
                    recent_scores.append((elapsed_seconds, current_score))
                    recent_live_motion += live_motion
                    recent_reference_motion += reference_motion
                    while recent_scores and recent_scores[0][0] <= last_verdict_at:
                        recent_scores.popleft()

                    if elapsed_seconds - last_verdict_at >= 2.5:
                        window_values = [0.0 if value is None else value for _timestamp, value in recent_scores]
                        window_average = (sum(window_values) / len(window_values)) if window_values else None
                        window_average = cap_score_for_missing_motion(
                            window_average,
                            recent_reference_motion,
                            recent_live_motion,
                        )
                        verdict_text, verdict_color = classify_score(window_average, config.difficulty)
                        verdict_popup = (verdict_text, verdict_color, window_average, elapsed_seconds)
                        if window_average is not None:
                            segment_score = score_segment_for_accuracy(window_average, config.difficulty) or window_average
                            if window_average >= get_difficulty_config(config.difficulty).good_threshold:
                                segment_score = max(segment_score, accuracy_percent)
                            score_total += segment_score
                            score_count += 1
                            accuracy_percent = score_total / score_count
                        last_verdict_at = elapsed_seconds
                        recent_scores.clear()
                        recent_live_motion = 0.0
                        recent_reference_motion = 0.0
                else:
                    recent_scores.clear()
                    recent_live_motion = 0.0
                    recent_reference_motion = 0.0

                overlay_points = build_overlay_points(
                    display_reference_frame.points,
                    live_points,
                    reference_aspect_ratio,
                    live_aspect_ratio,
                )
                draw_pose_overlay(
                    display_frame,
                    overlay_points,
                    (0, 255, 255),
                    frame_width,
                    frame_height,
                    alpha=0.35,
                    label="Ty",
                )

                # draw_status(display_frame, f"Wynik teraz: {current_score:.1f}" if current_score is not None else "Wynik teraz: brak", line=0)
                # draw_status(display_frame, f"Czas choreografii: {elapsed_seconds:.1f}s / {data.duration_seconds:.1f}s", line=3)
                # draw_status(
                #     display_frame,
                #     f"FPS: {fps:.1f} | Tryb: {config.difficulty} | Offset: {timing_offset * 1000:+.0f} ms",
                #     line=4,
                # )
                # draw_status(display_frame, "Q aby wyjsc", line=6)

                if elapsed_seconds >= 15.0 and accuracy_percent < 35.0:
                    game_over_frame = frame_for_window(window_name, display_frame)
                    draw_game_hud(game_over_frame, accuracy_percent, elapsed_seconds, data.duration_seconds)
                    cv2.imshow(window_name, game_over_frame)
                    audio_player.stop()
                    game_over_action = wait_for_game_over_action(window_name, display_frame, accuracy_percent)
                    if game_over_action == "menu":
                        return None
                    play_started_at = None
                    alignment_started_at = None
                    accuracy_percent = 100.0
                    score_total = 0.0
                    score_count = 0
                    previous_live_points = None
                    previous_reference_points = None
                    recent_scores.clear()
                    recent_live_motion = 0.0
                    recent_reference_motion = 0.0
                    last_verdict_at = 2.0
                    verdict_popup = None
                    video_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    previous_video_frame_index = 0
                    ok, restarted_first_frame = video_cap.read()
                    if ok:
                        first_video_frame = downscale_frame_for_performance(restarted_first_frame, GAME_MAX_FRAME_WIDTH)
                        last_video_frame = first_video_frame.copy()
                    previous_time = time.perf_counter()
                    continue

                if elapsed_seconds >= data.duration_seconds:
                    finish_frame = frame_for_window(window_name, display_frame)
                    draw_game_hud(finish_frame, accuracy_percent, elapsed_seconds, data.duration_seconds)
                    cv2.imshow(window_name, finish_frame)
                    show_final_result_animation(window_name, finish_frame, accuracy_percent)
                    break

            window_frame = frame_for_window(window_name, display_frame)
            if play_started_at is not None:
                current_elapsed = time.perf_counter() - play_started_at
                draw_game_hud(window_frame, accuracy_percent, current_elapsed, data.duration_seconds)
                if verdict_popup is not None:
                    popup_text, popup_color, popup_score, popup_started_at = verdict_popup
                    popup_elapsed = current_elapsed - popup_started_at
                    if popup_elapsed <= 1.35:
                        draw_verdict_popup(window_frame, popup_text, popup_color, popup_score, popup_elapsed)
                    else:
                        verdict_popup = None
            cv2.imshow(window_name, window_frame)
            key = cv2.waitKey(1) & 0xFF
            if key == 27:
                paused_elapsed = (time.perf_counter() - play_started_at) if play_started_at is not None else 0.0
                audio_player.stop()
                pause_action = wait_for_pause_action(window_name, display_frame)
                if pause_action == "menu":
                    return None
                if pause_action == "restart":
                    play_started_at = None
                    alignment_started_at = None
                    accuracy_percent = 100.0
                    score_total = 0.0
                    score_count = 0
                    previous_live_points = None
                    previous_reference_points = None
                    recent_scores.clear()
                    recent_live_motion = 0.0
                    recent_reference_motion = 0.0
                    last_verdict_at = 2.0
                    verdict_popup = None
                    video_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    previous_video_frame_index = 0
                    ok, restarted_first_frame = video_cap.read()
                    if ok:
                        first_video_frame = downscale_frame_for_performance(restarted_first_frame, GAME_MAX_FRAME_WIDTH)
                        last_video_frame = first_video_frame.copy()
                    previous_time = time.perf_counter()
                    continue
                if pause_action == "resume":
                    if not show_resume_countdown(window_name, display_frame, seconds=4):
                        return None
                    previous_live_points = None
                    previous_reference_points = None
                    recent_live_motion = 0.0
                    recent_reference_motion = 0.0
                    previous_time = time.perf_counter()
                    if play_started_at is not None:
                        play_started_at = time.perf_counter() - paused_elapsed
                        audio_enabled = audio_player.play(paused_elapsed)
                        if not audio_enabled:
                            print(f"Audio po pauzie nie wystartowalo: {audio_player.status_text()}")
                    continue
            if key in (ord("q"), ord("Q")):
                break
            if key in (ord("f"), ord("F")):
                is_fullscreen = not is_fullscreen
                cv2.setWindowProperty(
                    window_name,
                    cv2.WND_PROP_FULLSCREEN,
                    cv2.WINDOW_FULLSCREEN if is_fullscreen else cv2.WINDOW_NORMAL,
                )
    finally:
        audio_player.stop()
        live_camera.release()
        video_cap.release()
        cv2.destroyAllWindows()
        engine.close()

    final_average = accuracy_percent
    print(f"Koniec choreografii. Sredni wynik: {final_average:.1f}/100")
    return final_average


def classify_score(score: float | None, difficulty: str) -> tuple[str, tuple[int, int, int]]:
    difficulty_config = get_difficulty_config(difficulty)
    if score is None:
        return "Nie wykryto pozy", (0, 0, 255)
    if score >= difficulty_config.perfect_threshold:
        return "Perfect", (0, 220, 0)
    if score >= difficulty_config.good_threshold:
        return "Good", (0, 255, 255)
    if score >= difficulty_config.ok_threshold:
        return "Ok", (0, 165, 255)
    return "Miss", (0, 0, 255)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dance with me: skanowanie choreografii z lokalnego filmu i gra z kamerka.",
    )
    parser.add_argument("--camera-index", type=int, default=0, help="Numer kamerki do uzycia.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Przeskanuj lokalny film i zapisz choreografie do JSON.")
    scan_parser.add_argument("video", type=Path, help="Sciezka do lokalnego pliku wideo.")
    scan_parser.add_argument(
        "--output",
        type=Path,
        default=Path("choreography.json"),
        help="Sciezka pliku JSON z zeskanowana choreografia.",
    )
    scan_parser.add_argument(
        "--scan-fps",
        type=float,
        default=10.0,
        help="Docelowa liczba analizowanych klatek na sekunde podczas skanowania.",
    )

    play_parser = subparsers.add_parser("play", help="Graj przeciw zapisanej choreografii przez kamerke.")
    play_parser.add_argument(
        "choreography",
        type=Path,
        help="Sciezka do pliku JSON stworzonego w trybie scan.",
    )
    subparsers.add_parser("gui", help="Uruchom prosty interfejs okienkowy.")
    return parser


def main() -> None:
    parser = build_parser()
    if len(sys.argv) == 1:
        PySideDanceScorerGUI().run()
        return

    args = parser.parse_args()
    config = AppConfig(camera_index=args.camera_index)

    if args.command == "scan":
        scan_video(args.video, args.output, config, args.scan_fps)
    elif args.command == "play":
        play_choreography(args.choreography, config)
    elif args.command == "gui":
        PySideDanceScorerGUI().run()
    else:
        parser.error("Nieznana komenda.")


if __name__ == "__main__":
    main()
