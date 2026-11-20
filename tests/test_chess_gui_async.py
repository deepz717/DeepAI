"""Chess rules and GUI-worker regression tests; Stockfish is mocked."""
import threading
import time

import app


def test_multiple_legal_moves_and_illegal_move_preserve_board():
    game = app.ChessGame(initialize_engine=False)
    game.game_active = True
    game.get_best_move = lambda depth=10: None
    for san in ("e4", "e5", "Nf3", "Nc6", "Bb5", "a6"):
        game.user_color = "white" if game.board.turn == app.chess.WHITE else "black"
        result = game.make_move(san)
        assert f"Move played: {san}" in result
    before = game.board.fen()
    result = game.make_move("Qh9", is_ai=True)
    assert "Invalid move format" in result or "Illegal move" in result
    assert game.board.fen() == before
    assert len(game.move_history) == 6


def test_heuristic_ai_returns_a_legal_move_and_engine_timeout_falls_back():
    game = app.ChessGame(initialize_engine=False)
    move = game.get_best_move()
    assert move in game.board.legal_moves

    class TimedOutEngine:
        def play(self, board, limit):
            raise TimeoutError("mock Stockfish timeout")

    game.engine = TimedOutEngine()
    fallback = game.get_best_move()
    assert fallback in game.board.legal_moves


def test_stockfish_initialization_is_reused_and_search_has_a_time_limit(monkeypatch):
    calls = []
    class Engine:
        def play(self, board, limit):
            calls.append(("play", limit.time))
            return type("Result", (), {"move": next(iter(board.legal_moves))})()
        def quit(self): pass
    engine = Engine()
    monkeypatch.setattr(app.os.path, "exists", lambda path: path == "stockfish.exe")
    monkeypatch.setattr(app.chess.engine.SimpleEngine, "popen_uci",
                        lambda path, timeout: calls.append(("start", path, timeout)) or engine)
    game = app.ChessGame(initialize_engine=False)
    assert game.init_stockfish()
    assert game.init_stockfish()
    move = game.get_best_move()
    assert move in game.board.legal_moves
    assert calls == [("start", "stockfish.exe", 5.0), ("play", 1.5)]


def test_gui_move_runs_off_thread_serializes_and_keeps_tk_loop_free(monkeypatch):
    callbacks = []
    chat = []

    class FakeWindow:
        def __init__(self): self.closed = False
        def winfo_exists(self): return not self.closed
        def destroy(self): self.closed = True

    class FakeApp:
        def _post_to_ui(self, callback): callbacks.append(callback)
        def add_to_chat(self, text, is_user=False): chat.append(text)

    class Status:
        text = ""
        def configure(self, **kwargs): self.text = kwargs.get("text", "")

    fake_window = FakeWindow()
    monkeypatch.setattr(app, "chess_gui_window", fake_window)
    monkeypatch.setattr(app, "window_instance", FakeApp())
    game = app.ChessGame(initialize_engine=False)
    game.chess_status_label = Status()
    draws = []
    game._draw_board = lambda: draws.append(threading.get_ident())
    started, release = threading.Event(), threading.Event()

    def blocking_move(move):
        started.set()
        assert release.wait(2)
        return f"Move played: {move}"

    game.make_move = blocking_move
    main_thread = threading.get_ident()
    assert game._start_gui_move("e4")
    assert started.wait(1)
    assert not game._start_gui_move("d4")  # repeated click cannot duplicate work

    heartbeat = threading.Event()
    timer = threading.Timer(0.02, heartbeat.set)
    timer.start()
    assert heartbeat.wait(0.5)  # the caller/UI thread is not blocked
    release.set()
    game._gui_worker.join(1)
    assert len(callbacks) == 1
    callbacks.pop()()
    assert not game._gui_busy
    assert draws == [main_thread]
    assert chat and "Move played: e4" in chat[0]


def test_leaving_chess_during_worker_discards_stale_ui_result(monkeypatch):
    callbacks = []
    chat = []

    class FakeWindow:
        def __init__(self): self.closed = False
        def winfo_exists(self): return not self.closed
        def destroy(self): self.closed = True

    class FakeApp:
        def _post_to_ui(self, callback): callbacks.append(callback)
        def add_to_chat(self, text, is_user=False): chat.append(text)

    window = FakeWindow()
    monkeypatch.setattr(app, "chess_gui_window", window)
    monkeypatch.setattr(app, "window_instance", FakeApp())
    game = app.ChessGame(initialize_engine=False)
    game.chess_status_label = type("Status", (), {"configure": lambda *a, **k: None})()
    game._draw_board = lambda: (_ for _ in ()).throw(AssertionError("stale UI redraw"))
    started, release = threading.Event(), threading.Event()
    game.make_move = lambda move: (started.set(), release.wait(2), "late result")[-1]
    game._start_gui_move("e4")
    assert started.wait(1)
    game._close_gui_board()
    assert window.closed
    release.set()
    game._gui_worker.join(1)
    callbacks.pop()()
    assert not chat


def test_engine_shutdown_is_safe_when_no_engine_is_available():
    game = app.ChessGame(initialize_engine=False)
    game.quit_engine()
    assert game.engine is None


def test_leaving_chess_releases_engine_in_background(monkeypatch):
    quit_done = threading.Event()
    class Engine:
        def quit(self): quit_done.set()
    class Window:
        def destroy(self): pass
    monkeypatch.setattr(app, "chess_gui_window", Window())
    game = app.ChessGame(initialize_engine=False)
    game.engine = Engine()
    started = time.monotonic()
    game._close_gui_board()
    assert time.monotonic() - started < 0.1
    assert quit_done.wait(1)
    assert game.engine is None
