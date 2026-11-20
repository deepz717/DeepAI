"""Isolated checks for safe, offline DeepAI feature behavior."""
import sqlite3
import ast
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import app
import config
from ai.ollama import LocalLLM
from agents.code_agent import CodeAgent
from agents import register_all_agents
from core.agent_manager import AgentManager

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_pdf_chunking_normal_empty_and_invalid_inputs():
    chunks = app.PDFRAGHandler.chunk_text("one two three four", chunk_size=3, overlap=1)
    assert chunks == ["one two three", "three four"]
    assert app.PDFRAGHandler.chunk_text("  ") == []
    with pytest.raises(ValueError, match="smaller than"):
        app.PDFRAGHandler.chunk_text("text", chunk_size=2, overlap=2)
    with pytest.raises(ValueError, match="positive integer"):
        app.PDFRAGHandler.chunk_text("text", chunk_size=0)
    with pytest.raises(TypeError, match="string"):
        app.PDFRAGHandler.chunk_text(None)


def test_pdf_blank_page_extraction_and_search():
    if not app.PDF_AVAILABLE:
        pytest.skip("PyPDF2 optional dependency is not installed")
    with TemporaryDirectory(prefix="deepai-test-", dir=PROJECT_ROOT) as folder:
        pdf_path = Path(folder) / "blank.pdf"
        import PyPDF2
        writer = PyPDF2.PdfWriter()
        writer.add_blank_page(width=72, height=72)
        with pdf_path.open("wb") as stream:
            writer.write(stream)
        text, pages = app.PDFRAGHandler.extract_text(str(pdf_path))
        assert pages == 1 and text is None
        assert "scanned PDF" in app.PDFRAGHandler.last_error
        assert "No matches found" in app.PDFRAGHandler.search_pdf(str(pdf_path), "needle")


def test_project_rag_reads_only_given_fixture_and_finds_python_class():
    with TemporaryDirectory(prefix="deepai-test-", dir=PROJECT_ROOT) as folder:
        root = Path(folder)
        source = root / "sample.py"
        source.write_text("class SampleThing:\n    pass\n", encoding="utf-8")
        results = app.ProjectRAG.search_classes(str(root), "SampleThing")
        assert len(results) == 1
        assert results[0][0] == "sample.py"
        assert "class SampleThing" in results[0][2]
        assert app.ProjectRAG.find_class_definition(str(root), "SampleThing") == (None, None)


def test_chess_move_save_load_and_invalid_pgn_are_safe(monkeypatch):
    with TemporaryDirectory(prefix="deepai-test-", dir=PROJECT_ROOT) as folder:
        root = Path(folder)
        monkeypatch.setattr(app, "CHESS_SAVE_DIR", str(root))
        game = app.ChessGame(initialize_engine=False)
        game.game_active = True
        monkeypatch.setattr(game, "get_best_move", lambda depth=10: None)
        assert "e4" in game.make_move("e4")
        assert game.board.fullmove_number == 1
        filepath, filename = game.save_game("audit-test.json")
        restored = app.ChessGame(initialize_engine=False)
        ok, _ = restored.load_game(filename)
        assert ok and restored.board.fen() == game.board.fen()
        assert Path(filepath).is_relative_to(root)
        ok, message = restored.import_pgn("not a PGN")
        assert not ok and "Invalid PGN" in message
        ok, message = restored.import_pgn("1. e4 e5 *")
        assert ok and "2 moves" in message
        assert restored.move_history == ["e4", "e5"]
        ok, message = restored.load_game("..\\outside.json")
        assert not ok and "Invalid save filename" in message
        with pytest.raises(ValueError, match="simple file name"):
            restored.save_game("..\\outside.json")


def test_file_indexer_search_and_stats_with_temporary_database():
    with TemporaryDirectory(prefix="deepai-test-", dir=PROJECT_ROOT) as folder:
        root = Path(folder)
        database = root / "index.db"
        indexer = app.FileIndexer.__new__(app.FileIndexer)
        indexer.db_path = str(database)
        indexer.indexing = False
        indexer.index_progress = {"current": 0, "total": 0, "status": "idle"}
        indexer.last_scan = None
        indexer.init_db()
        payload = root / "fixture.txt"
        payload.write_text("not read by the index; only metadata inserted", encoding="utf-8")
        conn = sqlite3.connect(database)
        conn.execute(
            "INSERT INTO file_index(path, filename, extension, size, modified_time, is_indexed) VALUES (?, ?, ?, ?, ?, 1)",
            (str(payload), payload.name, ".txt", payload.stat().st_size, payload.stat().st_mtime),
        )
        conn.commit()
        conn.close()
        assert indexer.smart_search("fixture")[0][1] == "fixture.txt"
        assert "1 files indexed" in indexer.get_index_stats()


def test_file_indexer_skips_hidden_and_sensitive_file_names(monkeypatch):
    with TemporaryDirectory(prefix="deepai-test-", dir=PROJECT_ROOT) as folder:
        root = Path(folder)
        regular = root / "ordinary-report.txt"
        hidden_attr = root / "windows-hidden.txt"
        regular.touch()
        hidden_attr.touch()
        actual_stat = app.os.stat
        monkeypatch.setattr(
            app.os,
            "stat",
            lambda path: type("FileStat", (), {"st_file_attributes": 0x2})()
            if str(path) == str(hidden_attr)
            else actual_stat(path),
        )
        assert app.FileIndexer._is_indexable_file(str(regular))
        assert not app.FileIndexer._is_indexable_file(str(hidden_attr))
        assert not app.FileIndexer._is_indexable_file(str(root / ".hidden.txt"))
        assert not app.FileIndexer._is_indexable_file(str(root / "client_credentials.txt"))
        assert not app.FileIndexer._is_indexable_file(str(root / "identity.pem"))


def test_file_index_scan_filters_excluded_names_and_directories():
    with TemporaryDirectory(prefix="deepai-test-", dir=PROJECT_ROOT) as folder:
        root = Path(folder)
        scan = root / "scan"
        scan.mkdir()
        (scan / "ordinary.txt").write_text("metadata-only fixture", encoding="utf-8")
        (scan / ".hidden.txt").write_text("skip", encoding="utf-8")
        (scan / "my_passwords.txt").write_text("skip", encoding="utf-8")
        (scan / "signing-key.pem").write_text("skip", encoding="utf-8")
        for directory in ("node_modules", "__pycache__", ".git"):
            target = scan / directory
            target.mkdir()
            (target / "excluded.txt").touch()
        indexer = app.FileIndexer.__new__(app.FileIndexer)
        indexer.db_path = str(root / "index.db")
        indexer.indexing = False
        indexer.enabled = True
        indexer.index_progress = {"current": 0, "total": 0, "status": "idle"}
        indexer.last_scan = None
        indexer._state_lock = threading.RLock()
        indexer._pause_event = threading.Event()
        indexer._pause_event.set()
        indexer._cancel_event = threading.Event()
        indexer.index_paths = [str(scan)]
        indexer.init_db()
        assert indexer.incremental_index().startswith("📁 Indexing started")
        deadline = time.monotonic() + 5
        while indexer.indexing and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not indexer.indexing
        conn = sqlite3.connect(indexer.db_path)
        try:
            names = {row[0] for row in conn.execute("select filename from file_index")}
            previews = conn.execute("select count(*) from file_index where content_preview is not null").fetchone()[0]
        finally:
            conn.close()
        assert names == {"ordinary.txt"}
        assert previews == 0


def test_file_index_controls_disable_pause_and_clear_keep_database_file():
    with TemporaryDirectory(prefix="deepai-test-", dir=PROJECT_ROOT) as folder:
        root = Path(folder)
        indexer = app.FileIndexer.__new__(app.FileIndexer)
        indexer.db_path = str(root / "index.db")
        indexer.indexing = False
        indexer.enabled = True
        indexer.index_progress = {"current": 0, "total": 0, "status": "idle"}
        indexer.last_scan = None
        indexer._state_lock = threading.RLock()
        indexer._pause_event = threading.Event()
        indexer._pause_event.set()
        indexer._cancel_event = threading.Event()
        indexer.index_paths = []
        indexer.init_db()
        assert "No file indexing" in indexer.pause_indexing()
        assert "No file indexing" in indexer.resume_indexing()
        indexer.indexing = True
        assert "paused" in indexer.pause_indexing()
        assert not indexer._pause_event.is_set()
        assert "resumed" in indexer.resume_indexing()
        assert indexer._pause_event.is_set()
        indexer.indexing = False
        assert "disabled" in indexer.disable_indexing()
        assert "disabled" in indexer.incremental_index()
        assert "enabled" in indexer.enable_indexing()
        assert "started" in indexer.incremental_index()
        deadline = time.monotonic() + 5
        while indexer.indexing and time.monotonic() < deadline:
            time.sleep(0.01)
        conn = sqlite3.connect(indexer.db_path)
        conn.execute(
            "INSERT INTO file_index(path, filename, extension, size, modified_time, is_indexed) VALUES (?, ?, ?, ?, ?, 1)",
            (str(root / "keep.txt"), "keep.txt", ".txt", 5, 1.0),
        )
        conn.commit()
        conn.close()
        assert Path(indexer.db_path).is_file()
        assert "Cleared 1" in indexer.clear_index()
        assert Path(indexer.db_path).is_file()


def test_automatic_indexing_is_opt_in(monkeypatch):
    monkeypatch.delenv("DEEPAI_AUTO_INDEX_FILES", raising=False)
    assert config._env_bool("DEEPAI_AUTO_INDEX_FILES", False) is False
    monkeypatch.setenv("DEEPAI_AUTO_INDEX_FILES", "true")
    assert config._env_bool("DEEPAI_AUTO_INDEX_FILES", False) is True


def test_agent_registration_contains_specialists():
    manager = AgentManager(task_manager=object())
    register_all_agents(manager)
    expected = {"chess_agent", "file_agent", "system_agent", "vision_agent", "coding_agent", "research_agent", "memory_agent", "code_agent"}
    assert expected.issubset(set(manager.list_agents()))


def test_startup_constructs_only_one_reminder_worker():
    tree = ast.parse(Path(app.__file__).read_text(encoding="utf-8"))
    constructions = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ReminderWorker"
    ]
    assert len(constructions) == 1


def test_ollama_chat_sends_configured_model_and_handles_failures(monkeypatch):
    llm = LocalLLM(host="http://127.0.0.1:11434", text_model="configured-model")
    monkeypatch.setattr(llm, "is_alive", lambda refresh=False: True)

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"message": {"content": "Hello from test double"}}

    calls = []

    def post(url, json, timeout):
        calls.append((url, json, timeout))
        return Response()

    monkeypatch.setattr(llm._session, "post", post)
    result = llm.chat(
        [{"role": "user", "content": "Hello"}], timeout=9,
        max_tokens=80, stop=["\nAssistant:"],
    )
    assert result == "Hello from test double"
    assert calls[0][0].endswith("/api/chat")
    assert calls[0][1]["model"] == "configured-model"
    assert calls[0][2] == 9
    assert calls[0][1]["options"]["num_predict"] == 80
    assert calls[0][1]["options"]["stop"] == ["\nAssistant:"]
    assert calls[0][1]["stream"] is False
    assert calls[0][1]["think"] is False

    import requests
    monkeypatch.setattr(llm._session, "post", lambda *a, **k: (_ for _ in ()).throw(requests.Timeout()))
    assert llm.chat([{"role": "user", "content": "Hello"}]) == ""


def test_plus_menu_preserves_existing_tool_callbacks_and_groups():
    ui = app.DeepAIApp.__new__(app.DeepAIApp)
    calls = []
    ui._tools_menu = None
    ui._tools_menu_items = {"Upload image": lambda: calls.append("upload"),
                            "Play chess": lambda: calls.append("chess")}
    ui._close_tools_menu = lambda: calls.append("closed")
    ui._run_tool_menu_item("Upload image")
    ui._run_tool_menu_item("Play chess")
    assert calls == ["closed", "upload", "closed", "chess"]

    tree = ast.parse(Path(app.__file__).read_text(encoding="utf-8"))
    methods = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    source_all = Path(app.__file__).read_text(encoding="utf-8")
    source = ast.get_source_segment(source_all, methods["toggle_tools_menu"])
    for feature in ("Upload image", "Add PDF document", "Describe screen", "Voice input",
                    "Camera", "Analyze project", "Find files", "Play chess", "Chess board",
                    "Chess coach", "Show score", "Clear memory", "Clear chat"):
        assert feature in source
    menu_setup = ast.get_source_segment(source_all, methods["_init_tools_menu"])
    assert '"<Escape>"' in menu_setup and '"<Button-1>"' in menu_setup


def test_composer_enter_sends_but_shift_enter_inserts_newline():
    assert app.DeepAIApp._should_send_on_return(0)
    assert not app.DeepAIApp._should_send_on_return(0x0001)


def test_chat_bubble_formatting_removes_markdown_tokens_and_preserves_code():
    formatted = app.DeepAIApp._format_chat_text(
        "## Title\n**Bold** and `inline`.\n- item\n```python\nx = 1\n```"
    )
    assert formatted == "Title\nBold and inline.\n• item\nx = 1"


def test_code_validator_checks_long_lines_inside_code_not_explanation():
    explanation = "This explanation is intentionally long. " + ("detail " * 100)
    answer = f"```python\ndef add(a, b):\n    return a + b\n```\n{explanation}"
    assert CodeAgent._reject_if_bad(answer) is None
    long_code_line = "x = '" + ("a" * 510) + "'"
    assert "malformed" in CodeAgent._reject_if_bad(
        f"```python\n{long_code_line}\n```"
    )


def test_attachment_menu_reports_cancel_and_load_results():
    ui = app.DeepAIApp.__new__(app.DeepAIApp)
    messages = []
    ui.add_to_chat = lambda text, is_user=False: messages.append((text, is_user))
    ui._run_attachment_action(lambda: None, "Choose a file")
    ui._run_attachment_action(lambda: "Image loaded", "Choose a file")
    assert messages == [("Choose a file", False), ("Image loaded", False)]


def test_live_gui_plus_menu_interactions(monkeypatch):
    import os
    if os.environ.get("DEEPAI_RUN_GUI_TESTS") != "1":
        pytest.skip("Set DEEPAI_RUN_GUI_TESTS=1 to run the native Tk UI test")
    monkeypatch.setenv("DEEPAI_STAGED_SMOKE", "1")
    class NoopReminderWorker:
        def stop(self):
            pass

    monkeypatch.setattr(app, "ReminderWorker", NoopReminderWorker)
    monkeypatch.setattr(app, "check_ollama_alive", lambda: False)
    monkeypatch.setattr(app, "WAKE_WORD_ENABLED", False)
    ui = app.DeepAIApp()
    messages = []
    ui.add_to_chat = lambda text, is_user=False, **kwargs: messages.append((text, is_user))
    try:
        ui.root.update()
        ui.plus_btn.invoke()
        ui.root.update()
        assert ui._tools_menu and ui._tools_menu.winfo_exists()
        assert len(ui._tools_menu_buttons) == 20
        assert set(ui._tools_menu_buttons) == set(ui._tools_menu_items)
        assert isinstance(ui._tools_menu_body, app.ctk.CTkScrollableFrame)
        assert ui._tools_menu_buttons["Camera"].cget("state") == ("normal" if app.CV2_AVAILABLE else "disabled")
        assert ui._tools_menu_buttons["Volume controls"].cget("state") == ("normal" if ui._tools_menu_availability["Volume controls"][0] else "disabled")
        assert ui._tools_menu_buttons["Voice input"].cget("state") == ("normal" if ui._tools_menu_availability["Voice input"][0] else "disabled")
        assert "Stockfish is unavailable" in ui._tools_menu_buttons["Chess coach"].cget("text")
        assert ui._tools_menu.winfo_screenwidth() >= ui._tools_menu.winfo_rootx() + ui._tools_menu.winfo_width()
        assert ui._focus_menu_step(1) == "break"

        ui._close_tools_menu_escape()
        assert ui._tools_menu is None
        ui.plus_btn.invoke()
        ui.root.update()
        ui._run_tool_menu_item("Code assistance")
        assert "Help me write code:" in ui.composer.get("1.0", "end-1c")

        ui.upload_image_file = lambda: "Image loaded"
        ui._run_tool_menu_item("Upload image")
        assert messages[-1] == ("Image loaded", False)

        dispatched = []
        ui._dispatch_request = lambda prompt: dispatched.append(prompt)
        ui._dispatch_from_composer = lambda prompt: dispatched.append(prompt)
        ui._composer_focus_in()
        ui.composer.delete("1.0", "end")
        ui.composer.insert("1.0", "first line")
        ui.composer.focus_set()
        ui.root.update()
        ui._composer_return(type("ShiftEnterEvent", (), {"state": 0x0001})())
        ui.root.update()
        assert "first line\n" in ui.composer.get("1.0", "end-1c")
        ui.composer.insert("end-1c", "second line")
        ui.composer.focus_set()
        ui._composer_return(type("EnterEvent", (), {"state": 0})())
        deadline = time.monotonic() + 2
        while not dispatched and time.monotonic() < deadline:
            ui.root.update()
            time.sleep(0.01)
        assert dispatched == ["first line\nsecond line"]

        ui.plus_btn.invoke()
        ui.root.update()
        event = type("ClickEvent", (), {"widget": ui.root})()
        ui._close_tools_menu_outside(event)
        assert ui._tools_menu is None
    finally:
        ui.on_close()


def test_chat_messages_render_as_scrollable_bubbles_and_clear(monkeypatch):
    import os
    if os.environ.get("DEEPAI_RUN_GUI_TESTS") != "1":
        pytest.skip("Set DEEPAI_RUN_GUI_TESTS=1 to run the native Tk UI test")
    monkeypatch.setenv("DEEPAI_STAGED_SMOKE", "1")

    class NoopReminderWorker:
        def stop(self):
            pass

    monkeypatch.setattr(app, "ReminderWorker", NoopReminderWorker)
    monkeypatch.setattr(app, "check_ollama_alive", lambda: False)
    monkeypatch.setattr(app, "WAKE_WORD_ENABLED", False)
    ui = app.DeepAIApp()
    try:
        ui.root.update()
        assert isinstance(ui.chat_scroll, app.ctk.CTkScrollableFrame)
        initial_count = len(ui.chat_scroll.winfo_children())
        ui.add_to_chat("hello", is_user=True)
        ui.add_to_chat("**answer**\nsecond line", is_user=False)
        ui.root.update()
        bubbles = ui.chat_scroll.winfo_children()
        assert len(bubbles) == initial_count + 2
        assert bubbles[-2].winfo_children()[0].cget("text") == "You"
        assert bubbles[-1].winfo_children()[0].cget("text") == "DeepAI"
        image_path = PROJECT_ROOT / "_gui-preview-test.png"
        try:
            app.Image.new("RGB", (320, 180), color="navy").save(image_path)
            monkeypatch.setattr(app, "CURRENT_IMAGES", [str(image_path)])
            monkeypatch.setattr(app, "CURRENT_PDF", None)
            ui.update_preview_display()
            assert ui._preview_ctk_image is not None
            assert "_gui-preview-test.png" in app.PREVIEW_LABEL.cget("text")
        finally:
            image_path.unlink(missing_ok=True)
        ui.clear_chat()
        ui.root.update()
        assert ui.chat_scroll.winfo_children() == []
    finally:
        ui.on_close()


def test_live_gui_mouse_send_plus_enter_and_cancel_recovery(monkeypatch):
    import os
    if os.environ.get("DEEPAI_RUN_GUI_TESTS") != "1":
        pytest.skip("Set DEEPAI_RUN_GUI_TESTS=1 to run the native Tk UI test")
    monkeypatch.setenv("DEEPAI_STAGED_SMOKE", "1")

    class NoopReminderWorker:
        def stop(self): pass

    monkeypatch.setattr(app, "ReminderWorker", NoopReminderWorker)
    monkeypatch.setattr(app, "check_ollama_alive", lambda: False)
    monkeypatch.setattr(app, "WAKE_WORD_ENABLED", False)
    ui = app.DeepAIApp()
    first_started = threading.Event()
    second_completed = threading.Event()
    submitted = []

    def request(prompt):
        submitted.append(prompt)
        if prompt == "cancel this request":
            first_started.set()
            deadline = time.monotonic() + 5
            while ui._generation_cancel is not None and not ui._generation_cancel.is_set() and time.monotonic() < deadline:
                time.sleep(.01)
        else:
            second_completed.set()

    ui._dispatch_request = request

    def click(widget):
        # CTkButton binds its mouse callback to its internal canvas/labels.
        target = widget._canvas
        target.event_generate("<Button-1>", x=max(2, target.winfo_width() // 2),
                              y=max(2, target.winfo_height() // 2))
        ui.root.update()

    def pump_until(predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            ui.root.update()
            time.sleep(.01)
        ui.root.update()
        assert predicate()

    try:
        ui.root.update()
        click(ui.plus_btn)
        assert ui._tools_menu and ui._tools_menu.winfo_exists(), (
            f"plus click did not fire; mapped={ui.plus_btn.winfo_ismapped()}, "
            f"size={ui.plus_btn.winfo_width()}x{ui.plus_btn.winfo_height()}, "
            f"canvas={ui.plus_btn._canvas.winfo_width()}x{ui.plus_btn._canvas.winfo_height()}, "
            f"command={ui.plus_btn.cget('command')}"
        )
        ui._tools_menu_body._parent_canvas.yview_moveto(1.0)
        ui.root.update()
        ui._close_tools_menu_escape()
        assert ui._tools_menu is None

        ui._composer_focus_in()
        ui.composer.insert("1.0", "cancel this request")
        click(ui.send_btn)
        pump_until(first_started.is_set)
        click(ui.send_btn)  # Stop Generation
        pump_until(lambda: ui._generation_cancel is None)
        assert ui.send_btn.cget("text") == "↑"

        ui._composer_focus_in()
        ui.composer.delete("1.0", "end")
        ui.composer.insert("1.0", "line one")
        ui.composer.focus_set()
        ui._composer_return(type("ShiftEnterEvent", (), {"state": 0x0001})())
        assert "line one\n" in ui.composer.get("1.0", "end-1c")
        ui.composer.insert("end-1c", "line two")
        ui._composer_return(type("EnterEvent", (), {"state": 0})())
        pump_until(second_completed.is_set)
        pump_until(lambda: ui._generation_cancel is None)
        assert submitted == ["cancel this request", "line one\nline two"]
    finally:
        ui.on_close()
