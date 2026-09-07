"""Non-technical desktop UI for Supabase-backed survey analysis."""

from __future__ import annotations

import json
import os
import queue
import threading
from datetime import datetime
from pathlib import Path

import dearpygui.dearpygui as dpg

from .service import DesktopService, DesktopServiceError, DesktopTaskCancelled


class FeedbackInsightDesktop:
    def __init__(self, service=None, *, settings_path=None):
        self.service = service or DesktopService()
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker = None
        self.auto_update_consumed = False
        self.selected_surveys = set()
        self.surveys = []
        settings_root = Path(os.getenv("LOCALAPPDATA", "").strip() or Path.cwd()) / "FeedbackInsightHub"
        self.settings_path = Path(settings_path or settings_root / "desktop-settings.json")
        self.preferences = self._load_preferences()

    def _load_preferences(self):
        defaults = {"scan_on_open": True, "update_on_open": False, "run_ai_after_update": False}
        try:
            loaded = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return defaults
        return {
            "scan_on_open": bool(loaded.get("scan_on_open", defaults["scan_on_open"])),
            "update_on_open": bool(loaded.get("update_on_open", defaults["update_on_open"])),
            "run_ai_after_update": bool(
                loaded.get("run_ai_after_update", defaults["run_ai_after_update"])
            ),
        }

    def _save_preferences(self):
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.settings_path.with_suffix(".part")
        partial.write_text(
            json.dumps(self.preferences, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(partial, self.settings_path)

    @staticmethod
    def _font_path():
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        for name in ("msjh.ttc", "msjh.ttf", "mingliu.ttc"):
            candidate = fonts / name
            if candidate.is_file():
                return candidate
        return None

    def _configure_style(self):
        with dpg.theme() as theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (244, 246, 251))
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (255, 255, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (30, 40, 61))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (232, 235, 243))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (218, 224, 237))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (205, 214, 232))
                dpg.add_theme_color(dpg.mvThemeCol_CheckMark, (45, 73, 158))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (151, 163, 184))
                dpg.add_theme_color(dpg.mvThemeCol_TableHeaderBg, (218, 225, 238))
                dpg.add_theme_color(dpg.mvThemeCol_TableBorderStrong, (128, 141, 163))
                dpg.add_theme_color(dpg.mvThemeCol_TableBorderLight, (181, 190, 207))
                dpg.add_theme_color(dpg.mvThemeCol_TableRowBg, (255, 255, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TableRowBgAlt, (240, 243, 249))
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 24, 20)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 10, 7)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 6)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 10)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 9, 7)
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Button, (48, 73, 184))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (64, 91, 207))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (37, 57, 150))
            with dpg.theme_component(dpg.mvButton, enabled_state=False):
                # Busy-state buttons remain readable without looking active.
                dpg.add_theme_color(dpg.mvThemeCol_Text, (79, 91, 116))
                dpg.add_theme_color(dpg.mvThemeCol_Button, (207, 214, 228))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (207, 214, 228))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (207, 214, 228))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (168, 178, 198))
        dpg.bind_theme(theme)
        font_path = self._font_path()
        if font_path:
            with dpg.font_registry():
                font = dpg.add_font(str(font_path), 18)
            dpg.bind_font(font)

    def _build(self):
        with dpg.window(label="工作未完成", modal=True, show=False, tag="error_dialog", width=540, height=190):
            # Modal windows use a dark native background in some packaged
            # Dear PyGui builds, so keep the message contrast explicit.
            dpg.add_text("", tag="error_message", wrap=490, color=(238, 242, 252))
            dpg.add_spacer(height=14)
            dpg.add_button(label="關閉", callback=lambda: dpg.configure_item("error_dialog", show=False))

        with dpg.window(tag="main_window", label="Feedback Insight Hub｜本機分析工作台"):
            dpg.add_text("問卷分析工作台", color=(23, 32, 51))
            dpg.add_text(
                "問卷與工作狀態以網站資料庫為準；大型外部資料可使用本機固定版本。第一段更新統計與文字，第二段由 Gemini 產生解析。",
                color=(93, 102, 122), wrap=1080,
            )
            dpg.add_spacer(height=8)
            with dpg.group(horizontal=True):
                dpg.add_checkbox(
                    label="開啟時檢查最新狀態", default_value=self.preferences["scan_on_open"],
                    tag="scan_on_open", callback=self._preferences_changed,
                )
                dpg.add_checkbox(
                    label="開啟後自動更新未完成分析", default_value=self.preferences["update_on_open"],
                    tag="update_on_open", callback=self._preferences_changed,
                )
                dpg.add_checkbox(
                    label="第一段完成後執行 Gemini（使用 API 額度）",
                    default_value=self.preferences["run_ai_after_update"],
                    tag="run_ai_after_update", callback=self._preferences_changed,
                )

            with dpg.group(horizontal=True):
                dpg.add_button(label="重新整理狀態", tag="refresh_button", callback=self.refresh_surveys)
                dpg.add_button(label="更新勾選問卷", tag="selected_button", callback=self.update_selected)
                dpg.add_button(label="更新全部未更新", tag="all_button", callback=self.update_all)
                dpg.add_button(label="只執行勾選問卷 Gemini", tag="ai_button", callback=self.update_ai_selected)
                dpg.add_button(label="取消目前工作", tag="cancel_button", callback=self.cancel_work, enabled=False)
                dpg.add_button(label="開啟本機產物資料夾", callback=self.open_results)

            dpg.add_spacer(height=8)
            dpg.add_text("尚未連線檢查", tag="status_value", color=(61, 73, 99))
            dpg.add_progress_bar(default_value=0, tag="progress_value", width=-1, overlay="0%")
            dpg.add_text("問卷：—｜資料：—｜待更新：—", tag="summary_value", color=(61, 73, 99))
            dpg.add_spacer(height=6)

            with dpg.child_window(border=False):
                with dpg.table(
                    tag="survey_table", header_row=True, row_background=True,
                    borders_innerH=True, borders_outerH=True, borders_innerV=True,
                    borders_outerV=True, scrollY=True, freeze_rows=1,
                ):
                    dpg.add_table_column(label="選取", width_fixed=True, init_width_or_weight=55)
                    dpg.add_table_column(label="問卷", init_width_or_weight=2.2)
                    dpg.add_table_column(label="來源", init_width_or_weight=1.1)
                    dpg.add_table_column(label="題目", width_fixed=True, init_width_or_weight=55)
                    dpg.add_table_column(label="資料筆數", width_fixed=True, init_width_or_weight=90)
                    dpg.add_table_column(label="最新資料時間", init_width_or_weight=1.3)
                    dpg.add_table_column(label="統計／文字時間", init_width_or_weight=1.2)
                    dpg.add_table_column(label="Gemini 時間", init_width_or_weight=1.2)
                    dpg.add_table_column(label="第一段", init_width_or_weight=0.9)
                    dpg.add_table_column(label="第二段", init_width_or_weight=0.9)

    def _preferences_changed(self):
        self.preferences = {
            "scan_on_open": bool(dpg.get_value("scan_on_open")),
            "update_on_open": bool(dpg.get_value("update_on_open")),
            "run_ai_after_update": bool(dpg.get_value("run_ai_after_update")),
        }
        try:
            self._save_preferences()
        except OSError as exc:
            self._show_error(f"無法儲存桌面設定：{exc}")

    @staticmethod
    def _date(value):
        if not value:
            return "—"
        if isinstance(value, datetime):
            return value.astimezone().strftime("%Y-%m-%d %H:%M")
        return str(value)

    def _render_surveys(self, surveys):
        for child in dpg.get_item_children("survey_table", slot=1) or []:
            dpg.delete_item(child)
        self.surveys = list(surveys)
        known = {item.survey_id for item in self.surveys}
        self.selected_surveys.intersection_update(known)
        for item in self.surveys:
            state = "未更新" if item.needs_update else "最新"
            ai_state = "未更新" if item.needs_ai else "最新"
            if not item.response_count:
                state = "尚無資料"
                ai_state = "—"
            if item.latest_job_status in {"pending", "running"} and item.needs_update:
                state = "等待處理" if item.latest_job_status == "pending" else "分析中"
            elif item.latest_job_status == "failed" and item.needs_update:
                state = "上次失敗"
            if item.needs_update:
                ai_state = "等待第一段"
            elif item.latest_ai_job_status in {"pending", "running"} and item.needs_ai:
                ai_state = "等待處理" if item.latest_ai_job_status == "pending" else "分析中"
            elif item.latest_ai_job_status == "failed" and item.needs_ai:
                ai_state = "上次失敗"
            with dpg.table_row(parent="survey_table"):
                dpg.add_checkbox(
                    default_value=item.survey_id in self.selected_surveys,
                    callback=self._selection_changed, user_data=item.survey_id,
                )
                dpg.add_text(item.title, wrap=280)
                dpg.add_text(item.source)
                dpg.add_text(str(item.question_count))
                dpg.add_text(f"{item.response_count:,}")
                dpg.add_text(self._date(item.latest_data_at))
                dpg.add_text(self._date(item.latest_analysis_at))
                dpg.add_text(self._date(item.latest_ai_at))
                dpg.add_text(state, color=(184, 88, 45) if item.needs_update else (40, 125, 82))
                dpg.add_text(ai_state, color=(184, 88, 45) if item.needs_ai or item.needs_update else (40, 125, 82))
        total = sum(item.response_count for item in self.surveys)
        stale = sum(item.needs_update for item in self.surveys)
        stale_ai = sum(item.needs_ai for item in self.surveys)
        dpg.set_value(
            "summary_value",
            f"問卷：{len(self.surveys):,}｜資料：{total:,} 筆｜待第一段：{stale:,}｜待 Gemini：{stale_ai:,}",
        )

    def _selection_changed(self, sender, app_data, user_data):
        if app_data:
            self.selected_surveys.add(int(user_data))
        else:
            self.selected_surveys.discard(int(user_data))

    @staticmethod
    def _set_progress(stage, percent):
        dpg.set_value("status_value", stage)
        dpg.set_value("progress_value", max(0, min(percent / 100, 1)))
        dpg.configure_item("progress_value", overlay=f"{percent}%")

    def _set_busy(self, busy):
        for tag in ("refresh_button", "selected_button", "all_button", "ai_button"):
            dpg.configure_item(tag, enabled=not busy)
        dpg.configure_item("cancel_button", enabled=busy)

    def _start(self, operation, callable_):
        if self.worker and self.worker.is_alive():
            return
        self.cancel_event.clear()
        self._set_progress("準備工作…", 0)
        self._set_busy(True)

        def target():
            try:
                value = callable_(
                    cancel_requested=self.cancel_event.is_set,
                    progress=lambda stage, percent: self.events.put(("progress", stage, percent)),
                )
                self.events.put(("done", operation, value))
            except DesktopTaskCancelled as exc:
                self.events.put(("cancelled", str(exc)))
            except DesktopServiceError as exc:
                self.events.put(("error", str(exc)))
            except Exception as exc:
                self.events.put(("error", f"未預期的錯誤（{type(exc).__name__}）"))

        self.worker = threading.Thread(target=target, daemon=False)
        self.worker.start()

    def refresh_surveys(self):
        self._start("refresh", self.service.list_surveys)

    def update_selected(self):
        if not self.selected_surveys:
            self._show_error("請先勾選至少一份問卷。")
            return
        selected = tuple(sorted(self.selected_surveys))
        self._start(
            "update",
            lambda **kwargs: self.service.update_two_stage_analysis(
                selected,
                include_ai=self.preferences["run_ai_after_update"],
                **kwargs,
            ),
        )

    def update_all(self):
        self._start(
            "update",
            lambda **kwargs: self.service.update_two_stage_analysis(
                include_ai=self.preferences["run_ai_after_update"],
                **kwargs,
            ),
        )

    def update_ai_selected(self):
        if not self.preferences["run_ai_after_update"]:
            self._show_error("請先勾選「第一段完成後執行 Gemini」，確認會使用 API 額度。")
            return
        if not self.selected_surveys:
            self._show_error("請先勾選至少一份問卷。")
            return
        selected = tuple(sorted(self.selected_surveys))
        self._start(
            "ai",
            lambda **kwargs: self.service.update_ai_surveys(
                selected,
                allow_paid_ai=True,
                **kwargs,
            ),
        )

    def cancel_work(self):
        self.cancel_event.set()
        try:
            self.service.cancel_active_update()
        except Exception:
            pass
        dpg.set_value("status_value", "正在安全停止；不會發布未完成結果…")
        dpg.configure_item("cancel_button", enabled=False)

    def open_results(self):
        self.service.database_output_root.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(self.service.database_output_root))
        except OSError as exc:
            self._show_error(str(exc))

    @staticmethod
    def _show_error(message):
        dpg.set_value("error_message", message)
        dpg.configure_item("error_dialog", show=True)

    def _drain_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "progress":
                    self._set_progress(event[1], event[2])
                    continue
                if self.worker:
                    self.worker.join(timeout=0.1)
                    self.worker = None
                if kind == "done":
                    operation, value = event[1], event[2]
                    self._set_busy(False)
                    if operation == "refresh":
                        self._render_surveys(value)
                        self._set_progress("資料庫狀態已更新", 100)
                        if (
                            self.preferences["update_on_open"]
                            and not self.auto_update_consumed
                            and any(item.needs_update for item in value)
                        ):
                            self.auto_update_consumed = True
                            self.update_all()
                    elif operation == "update":
                        deterministic = value.deterministic
                        ai = value.ai
                        note = (
                            f"第一段更新 {deterministic.updated_count} 份、處理 {deterministic.input_rows:,} 筆"
                            if not value.cancelled else "更新已安全取消"
                        )
                        if ai is not None and not value.cancelled:
                            note += f"；Gemini 更新 {ai.updated_count} 份"
                        self._set_progress(note, 100 if not value.cancelled else 0)
                        self.refresh_surveys()
                    else:
                        note = (
                            f"Gemini 已更新 {value.updated_count} 份問卷"
                            if not value.cancelled else "Gemini 更新已安全取消"
                        )
                        self._set_progress(note, 100 if not value.cancelled else 0)
                        self.refresh_surveys()
                elif kind == "cancelled":
                    self._set_progress(event[1], 0)
                    self._set_busy(False)
                elif kind == "error":
                    self._set_progress("工作失敗", 0)
                    self._set_busy(False)
                    self._show_error(event[1])
        except queue.Empty:
            return

    def run(self):
        dpg.create_context()
        try:
            self._configure_style()
            self._build()
            dpg.create_viewport(
                # Keep the native Windows title ASCII-only; localized text stays
                # inside the UTF-8/CJK-font-controlled application canvas.
                title="Feedback Insight Hub", width=1380, height=760,
                min_width=1080, min_height=620,
            )
            dpg.setup_dearpygui()
            dpg.show_viewport()
            dpg.set_primary_window("main_window", True)
            if self.preferences["scan_on_open"]:
                self.refresh_surveys()
            while dpg.is_dearpygui_running():
                self._drain_events()
                dpg.render_dearpygui_frame()
        finally:
            self.cancel_event.set()
            try:
                self.service.cancel_active_update()
            except Exception:
                pass
            if self.worker and self.worker.is_alive():
                self.worker.join(timeout=60)
            dpg.destroy_context()


def main():
    FeedbackInsightDesktop().run()
