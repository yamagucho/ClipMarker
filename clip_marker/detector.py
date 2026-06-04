"""
detector.py  ─  Kill / event detection core
Splatoon 2 / 3:
  - キル: 画面下部中央の「○○をたおした！」黒帯バナーを検出
    ROI内の暗部比率とフレーム差分の組み合わせで誤検出を抑制
  - デス: 全画面の急激な暗転（リスポーン暗転）を検出
"""

import cv2
import numpy as np
from dataclasses import dataclass
from typing import Callable


@dataclass
class MarkerEvent:
    time_sec: float
    event_type: str   # "kill", "death", "manual"
    confidence: float = 1.0
    label: str = ""

    def __post_init__(self):
        if not self.label:
            self.label = self.event_type


# ─── ROI definitions (normalised 0-1)  [x, y, w, h] ─────────────────────────

SPLATOON_ROIS = {
    # 「○○をたおした！」バナー: 画面下部中央の黒帯
    # 1920x1080基準: x≈290~1630 (15%~85%), y≈900~1010 (83%~93%)
    "kill_banner": (0.15, 0.83, 0.70, 0.10),

    # デス検出用: 画面中央の広めの領域
    "death_zone":  (0.25, 0.25, 0.50, 0.50),
}


def _crop_roi(frame: np.ndarray, roi: tuple) -> np.ndarray:
    h, w = frame.shape[:2]
    x  = int(roi[0] * w)
    y  = int(roi[1] * h)
    rw = int(roi[2] * w)
    rh = int(roi[3] * h)
    return frame[y:y+rh, x:x+rw]


def _gray(frame: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


class SplatoonDetector:
    """
    フレームを順番に feed() に渡すと list[MarkerEvent] を返す。
    空リストのときはイベントなし。

    キル検出アルゴリズム:
      1. kill_banner ROI を切り出す
      2. 暗部比率 (輝度<50のピクセル割合) を計算
         → バナー出現時は黒背景が大半を占めるため 0.55 以上になる
      3. 前フレームとのフレーム差分平均を計算
         → バナーが新たに出現したフレームで大きな差分が生じる
      4. 両条件を AND で判定 + クールダウン
    """

    def __init__(
        self,
        kill_dark_ratio: float = 0.55,    # バナー内の暗部ピクセル比率閾値
        kill_diff_threshold: float = 12.0, # フレーム差分の閾値（出現タイミング検出）
        death_dark_threshold: float = 30.0,# デス検出: 輝度ドロップ量
        cooldown_sec: float = 3.0,
    ):
        self.kill_dark_ratio     = kill_dark_ratio
        self.kill_diff_threshold = kill_diff_threshold
        self.death_dark_threshold = death_dark_threshold
        self.cooldown_sec        = cooldown_sec

        self._prev_banner: np.ndarray | None = None
        self._prev_death_mean: float | None  = None

        self._last_kill_t:  float = -999.0
        self._last_death_t: float = -999.0

    # ── バナー検出 ────────────────────────────────────────────────────────────
    def _detect_kill(self, frame: np.ndarray, t: float) -> MarkerEvent | None:
        roi  = _crop_roi(frame, SPLATOON_ROIS["kill_banner"])
        g    = _gray(roi)
        small = cv2.resize(g, (160, 24))  # 解像度正規化

        # 条件1: 暗部比率
        dark_ratio = float((small < 50).sum()) / small.size

        # 条件2: 前フレームとの差分（バナー出現瞬間を捉える）
        diff_mean = 0.0
        if self._prev_banner is not None:
            diff_mean = float(cv2.absdiff(small, self._prev_banner).mean())

        self._prev_banner = small.copy()

        if dark_ratio >= self.kill_dark_ratio and diff_mean >= self.kill_diff_threshold:
            if t - self._last_kill_t > self.cooldown_sec:
                conf = min(1.0, dark_ratio * (diff_mean / 30.0))
                self._last_kill_t = t
                return MarkerEvent(t, "kill", round(conf, 3))
        return None

    # ── デス検出 ──────────────────────────────────────────────────────────────
    def _detect_death(self, frame: np.ndarray, t: float) -> MarkerEvent | None:
        roi  = _crop_roi(frame, SPLATOON_ROIS["death_zone"])
        mean = float(_gray(roi).mean())

        result = None
        if self._prev_death_mean is not None:
            drop = self._prev_death_mean - mean
            if drop >= self.death_dark_threshold and mean < 35:
                if t - self._last_death_t > self.cooldown_sec:
                    conf = min(1.0, drop / 80.0)
                    self._last_death_t = t
                    result = MarkerEvent(t, "death", round(conf, 3))

        self._prev_death_mean = mean
        return result

    # ── メイン ────────────────────────────────────────────────────────────────
    def feed(self, frame: np.ndarray, time_sec: float) -> list[MarkerEvent]:
        events: list[MarkerEvent] = []
        kill = self._detect_kill(frame, time_sec)
        if kill:
            events.append(kill)
        death = self._detect_death(frame, time_sec)
        if death:
            events.append(death)
        return events


def analyse_video(
    path: str,
    detector: SplatoonDetector,
    sample_fps: float = 10.0,
    progress_cb: Callable[[float], None] | None = None,
) -> list[MarkerEvent]:
    """
    動画を sample_fps レートでフレームサンプリングして解析。
    progress_cb(0.0~1.0) は定期的に呼ばれる。
    """
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise IOError(f"Cannot open video: {path}")

    video_fps   = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_step  = max(1, int(video_fps / sample_fps))

    events: list[MarkerEvent] = []
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % frame_step == 0:
            t = frame_idx / video_fps
            events.extend(detector.feed(frame, t))
            if progress_cb and total_frames > 0:
                progress_cb(frame_idx / total_frames)
        frame_idx += 1

    cap.release()
    if progress_cb:
        progress_cb(1.0)
    return events
