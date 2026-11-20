"""Offline regression tests for the streaming and core feature upgrades."""
import json
import threading
import types

import app
from ai.ollama import LocalLLM
from agents.head_agent import HeadAgent
from agents.system_agent import SystemAgent


class _StreamResponse:
    status_code = 200

    def __init__(self, lines):
        self.lines = lines
        self.closed = False

    def iter_lines(self, decode_unicode=True):
        yield from self.lines

    def close(self):
        self.closed = True


def _line(message=None, **fields):
    return json.dumps({"message": message or {}, **fields}).encode()


def _stream_llm(monkeypatch, lines):
    llm = LocalLLM(host="http://127.0.0.1:11434", text_model="test")
    monkeypatch.setattr(llm, "is_alive", lambda refresh=False: True)
    response = _StreamResponse(lines)
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return response
    monkeypatch.setattr(llm._session, "post", post)
    return llm, response, calls


def test_ollama_stream_assembles_chunks_and_ignores_reasoning(monkeypatch):
    llm, response, calls = _stream_llm(monkeypatch, [
        _line({"thinking": "private chain"}),
        _line({"content": "Hello "}),
        b"not-json",
        _line({"content": "from DeepAI."}),
        _line({"content": ""}, done=True),
    ])
    emitted = []
    result = llm.chat([{"role": "user", "content": "Hello"}], on_chunk=emitted.append)
    assert result == "Hello from DeepAI."
    assert "".join(emitted) == result
    assert "private" not in result
    assert calls[0][1]["json"]["stream"] is True
    assert calls[0][1]["json"]["think"] is False
    assert response.closed


def test_ollama_stream_cancellation_and_interruption_return_only_safe_text(monkeypatch):
    event = threading.Event()
    llm, _, _ = _stream_llm(monkeypatch, [_line({"content": "A complete sentence. "}), _line({"content": "later"})])
    emitted = []
    def cancel_after_first(chunk):
        emitted.append(chunk)
        event.set()
    text = llm.chat([{"role": "user", "content": "test"}], on_chunk=cancel_after_first, cancel_event=event)
    assert text == "".join(emitted)
    assert "private reasoning" not in text


def test_stream_request_handle_closes_active_response():
    handle = app.StreamRequestHandle()
    response = _StreamResponse([])
    handle.set_response(response)
    handle.cancel()
    assert response.closed
    handle.set_response(None)


def test_stream_request_handle_closes_response_arriving_after_cancel():
    handle = app.StreamRequestHandle()
    handle.cancel()
    response = _StreamResponse([])
    handle.set_response(response)
    assert response.closed


def test_headagent_threads_stream_callback_and_handle_to_chat(monkeypatch):
    model = types.SimpleNamespace(chat_model="test-chat", text_model="test-text")
    calls = []
    def chat(messages, **kwargs):
        calls.append(kwargs)
        kwargs["on_chunk"]("Hello.")
        return "Hello."
    model.chat = chat
    monkeypatch.setattr("core.memory.get_memory", lambda: types.SimpleNamespace(
        recent_context=lambda n=8: "", long_term_context_string=lambda: ""))
    head = HeadAgent.__new__(HeadAgent)
    head._llm = model
    head._conversation_lock = threading.RLock()
    head._pending_confirmation = None
    head._conversation_state = None
    output = []
    handle = app.StreamRequestHandle()
    result = head.run("Hello", stream_callback=output.append,
                      cancel_event=threading.Event(), request_handle=handle)
    assert result == "Hello."
    assert output == ["Hello."]
    assert calls[0]["request_handle"] is handle


def test_non_agentic_dispatch_uses_streaming_direct_path(monkeypatch):
    import config
    ui = app.DeepAIApp.__new__(app.DeepAIApp)
    ui._generation_cancel = threading.Event()
    ui._generation_request = app.StreamRequestHandle()
    calls = []
    ui.process_and_send_response_streaming = lambda prompt: calls.append(prompt)
    ui.process_and_send_response = lambda prompt: (_ for _ in ()).throw(AssertionError("legacy nonstream path used"))
    monkeypatch.setattr(config, "USE_AGENTIC", False)
    monkeypatch.setattr(app, "CURRENT_PDF", None)
    monkeypatch.setattr(app, "file_indexer", types.SimpleNamespace(
        get_index_stats=lambda: "", rebuild_index=lambda: "",
        pause_indexing=lambda: "", resume_indexing=lambda: "",
        disable_indexing=lambda: "", enable_indexing=lambda: "",
        clear_index=lambda: ""))
    monkeypatch.setattr(app, "logging", types.SimpleNamespace(exception=lambda *a: None))
    monkeypatch.setattr(ui, "update_status", lambda *a: None)
    ui._dispatch_request("Explain Python")
    assert calls == ["Explain Python"]


def test_interrupted_stream_emits_visible_network_notice(monkeypatch):
    class Broken(_StreamResponse):
        def iter_lines(self, decode_unicode=True):
            yield _line({"content": "An answer has begun and is sufficiently long. "})
            raise RuntimeError("connection reset")
    llm = LocalLLM(host="http://127.0.0.1:11434", text_model="test")
    monkeypatch.setattr(llm, "is_alive", lambda refresh=False: True)
    response = Broken([])
    monkeypatch.setattr(llm._session, "post", lambda *a, **k: response)
    emitted = []
    result = llm.chat([{"role": "user", "content": "test"}], on_chunk=emitted.append)
    assert "[Response interrupted by a network error.]" in result
    assert result == "".join(emitted)
    assert response.closed


def test_english_drift_detector_handles_15_prompt_categories():
    prompts = [
        "Hello", "Who are you?", "Explain Python", "Write Python code",
        "Summarize a document", "What is the weather?", "Tell me a joke",
        "How do I sort a list?", "Explain recursion", "What is a database?",
        "Help debug this error", "Compare lists and tuples", "What is a class?",
        "How are you?", "Explain this code",
    ]
    english = "Python is a programming language used to build many kinds of software."
    assert all(not HeadAgent._unexpected_non_english(english, prompt) for prompt in prompts)
    assert HeadAgent._unexpected_non_english("El programa funciona bien y tiene una lista para guardar datos.", "Explain the program")
    assert not HeadAgent._unexpected_non_english("La respuesta es correcta.", "Answer in Spanish")
    assert not HeadAgent._unexpected_non_english("Use `print('hello')` in the Python code.", "Explain Python")


def test_pdf_retrieval_ranks_relevant_chunks_and_handles_no_match():
    chunks = ["Introduction to forests and trees", "A detailed section about Python classes", "Conclusion about databases"]
    assert app.PDFRAGHandler.retrieve(chunks, "Explain Python classes", limit=1) == [chunks[1]]
    assert app.PDFRAGHandler.retrieve(chunks, "unmatched zzq", limit=2) == []


def test_pdf_extracts_every_page_and_preserves_blank_pages(monkeypatch):
    class Page:
        def __init__(self, text): self.text = text
        def extract_text(self): return self.text
    class Reader:
        pages = [Page("page one"), Page(None), Page("page three")]
        metadata = {"/Title": "Sample", "/Author": "Example"}
    monkeypatch.setattr(app, "PDF_AVAILABLE", True)
    monkeypatch.setattr(app, "PyPDF2", types.SimpleNamespace(PdfReader=lambda stream: Reader()), raising=False)
    path = __import__("pathlib").Path(__file__).with_name("_mock-multipage.pdf")
    try:
        path.write_bytes(b"mock PDF container")
        text, count = app.PDFRAGHandler.extract_text(str(path))
        assert count == 3
        assert text.count("\n") >= 3 and "page three" in text
        assert "[PDF page 3]" in text
        assert app.PDFRAGHandler.metadata(path) == {"Title": "Sample", "Author": "Example"}
    finally:
        path.unlink(missing_ok=True)


def test_pdf_with_only_scanned_pages_reports_no_text(monkeypatch):
    class Page:
        def extract_text(self): return None
    class Reader:
        pages = [Page(), Page()]
    monkeypatch.setattr(app, "PDF_AVAILABLE", True)
    monkeypatch.setattr(app, "PyPDF2", types.SimpleNamespace(PdfReader=lambda stream: Reader()), raising=False)
    path = __import__("pathlib").Path(__file__).with_name("_mock-scanned.pdf")
    try:
        path.write_bytes(b"mock PDF container")
        text, count = app.PDFRAGHandler.extract_text(str(path))
        assert text is None
        assert count == 2
        assert "scanned PDF" in app.PDFRAGHandler.last_error
    finally:
        path.unlink(missing_ok=True)


def test_llava_image_payload_encodes_actual_image(monkeypatch):
    from PIL import Image
    import base64
    llm = LocalLLM(host="http://127.0.0.1:11434", vision_model="llava:test")
    monkeypatch.setattr(llm, "is_alive", lambda refresh=False: True)
    monkeypatch.setattr(llm, "vision_available", lambda refresh=False: True)
    payloads = []
    monkeypatch.setattr(llm, "_post_generate", lambda payload, timeout: payloads.append(payload) or {"response": "A real test image."})
    assert llm.vision(Image.new("RGB", (3, 2), "red"), "Describe this image") == "A real test image."
    payload = payloads[0]
    assert payload["model"] == "llava:test"
    assert payload["stream"] is False
    assert base64.b64decode(payload["images"][0]).startswith(b"\x89PNG\r\n\x1a\n")


def test_tts_worker_initializes_off_thread_and_plays_long_text(monkeypatch):
    finished = threading.Event()
    calls = []
    class Engine:
        def getProperty(self, name): return [object()] if name == "voices" else 150
        def setProperty(self, *args): pass
        def say(self, text): calls.append(("say", text))
        def runAndWait(self): calls.append(("run",)); finished.set()
        def stop(self): calls.append(("stop",))
    monkeypatch.setattr(app.pyttsx3, "init", lambda **kwargs: Engine())
    worker = app.SpeechWorker()
    assert worker.ready.wait(2) and worker.engine is not None
    text = "A long assistant response. " * 200
    assert worker.speak(text)
    assert finished.wait(2)
    assert calls[0] == ("say", text)
    worker.stop()


def test_tts_engine_failure_is_graceful_and_not_advertised_as_ready(monkeypatch):
    monkeypatch.setattr(app.pyttsx3, "init", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("SAPI unavailable")))
    worker = app.SpeechWorker()
    assert worker.ready.wait(2)
    assert worker.engine is None
    assert "SAPI unavailable" in worker.error
    assert worker.speak("test") is False


def test_recognition_requests_english_and_preserves_case(monkeypatch):
    worker = app.AudioWorker()
    seen = []
    class Recognizer:
        def recognize_google(self, audio, language):
            seen.append(language)
            return "DeepAI Python"
    worker.recognizer = Recognizer()
    assert worker.recognize(object()) == "DeepAI Python"
    assert seen == ["en-US"]


def test_voice_transcript_is_inserted_for_review_not_dispatched(monkeypatch):
    monkeypatch.setattr(app, "WAKE_WORD_ENABLED", False)
    ui = app.DeepAIApp.__new__(app.DeepAIApp)
    events = []
    class Composer:
        def delete(self, *args): events.append(("delete", args))
        def insert(self, *args): events.append(("insert", args))
        def focus_set(self): events.append(("focus",))
    ui.composer = Composer()
    ui._composer_focus_in = lambda: events.append(("focus_in",))
    ui._composer_placeholder_active = True
    class Root:
        def after(self, delay, callback): callback()
    ui.root = Root()
    ui.audio_worker = types.SimpleNamespace(
        listen_once=lambda **kwargs: object(), recognize=lambda audio: "Explain Python")
    ui.update_voice_btn_state = lambda state: None
    ui.update_status = lambda text: events.append(("status", text))
    ui.mic_toggle_active = False
    ui.tts_engine = None
    ui.add_to_chat = lambda *args, **kwargs: events.append(("chat", args))
    ui._dispatch_request = lambda text: events.append(("dispatch", text))
    ui.voice_button_handler()
    # Worker is asynchronous; wait for the composer insertion.
    import time
    deadline = time.monotonic() + 2
    while not any(event[0] == "insert" for event in events) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ("insert", ("1.0", "Explain Python")) in events
    assert not any(event[0] == "dispatch" for event in events)


def test_composer_binds_enter_on_actual_text_widget_and_shift_inserts_newline():
    events = []
    class TextWidget:
        def bind(self, sequence, callback, add=None):
            events.append((sequence, callback, add))
    class Composer:
        _textbox = TextWidget()
        def insert(self, *args): pass
        def configure(self, **kwargs): pass
        def get(self, *args): return ""
    ui = app.DeepAIApp.__new__(app.DeepAIApp)
    ui._composer_placeholder = "Message DeepAI..."
    ui._composer_focus_in = lambda event=None: None
    ui._composer_focus_out = lambda event=None: None
    ui._composer_return = lambda event: "break"
    ui._bind_composer(Composer())
    bindings = {sequence for sequence, _callback, _add in events}
    assert {"<FocusIn>", "<FocusOut>", "<Return>", "<KP_Enter>"} <= bindings


def test_send_keeps_focused_composer_empty_for_next_keyboard_message(monkeypatch):
    submitted = []
    class Composer:
        def __init__(self): self.value = "Message A"; self.focused = False
        def get(self, *_args): return self.value
        def delete(self, *_args): self.value = ""
        def focus_set(self): self.focused = True
    class Button:
        def configure(self, **kwargs): pass
    class Thread:
        def __init__(self, target, args, daemon): self.target, self.args = target, args
        def start(self): submitted.extend(self.args)
    monkeypatch.setattr(app.threading, "Thread", Thread)
    ui = app.DeepAIApp.__new__(app.DeepAIApp)
    ui.composer = Composer()
    ui._composer_placeholder_active = False
    ui._generation_cancel = None
    ui._generation_request = None
    ui.add_to_chat = lambda *_a, **_k: None
    ui._create_stream_bubble = lambda: (None, None)
    ui.send_btn = Button()
    ui._dispatch_from_composer = lambda prompt: None
    ui.send()
    assert ui.composer.value == ""
    assert ui.composer.focused
    assert not ui._composer_placeholder_active
    ui._generation_cancel = None  # completed first request
    ui._generation_request = None
    ui.composer.value = "Message B"
    ui.send()
    assert submitted == ["Message A", "Message B"]


def test_web_research_menu_callback_is_connected_to_search_action():
    import inspect
    source = inspect.getsource(app.DeepAIApp._init_tools_menu)
    assert '"Web research": self.start_web_research' in source
    assert hasattr(app.DeepAIApp, "_run_web_research")


def test_camera_preview_close_releases_capture(monkeypatch, capsys):
    class Capture:
        def __init__(self): self.released = False
        def isOpened(self): return True
        def release(self): self.released = True
    capture = Capture()
    class DummyFrame:
        def __init__(self, *a, **k): pass
        def pack(self, *a, **k): pass
        def configure(self, *a, **k): pass
        def grid(self, *a, **k): pass
    windows = []
    class DummyWindow:
        def __init__(self, *a, **k): self.protocol_handler = None; windows.append(self)
        def title(self, *a): pass
        def geometry(self, *a): pass
        def protocol(self, _name, callback): self.protocol_handler = callback
        def destroy(self): pass
        def after(self, *a): pass
        def winfo_exists(self): return True
        def mainloop(self): pass
    class DummyCTk:
        CTkToplevel = DummyWindow
        CTkLabel = DummyFrame
        CTkFrame = DummyFrame
        CTkButton = DummyFrame
        CTkImage = lambda *a, **k: object()
    monkeypatch.setattr(app, "CV2_AVAILABLE", True)
    dummy_image = types.SimpleNamespace(copy=lambda: types.SimpleNamespace(thumbnail=lambda *a: None, size=(20, 20)))
    monkeypatch.setattr(app, "Image", types.SimpleNamespace(fromarray=lambda value: dummy_image))
    monkeypatch.setattr(app, "cv2", types.SimpleNamespace(VideoCapture=lambda *a: capture), raising=False)
    fake_cv2 = types.SimpleNamespace(VideoCapture=lambda *a: capture, CAP_DSHOW=0,
                                     cvtColor=lambda *a: object(),
                                     COLOR_BGR2RGB=1)
    monkeypatch.setattr(app, "cv2", fake_cv2, raising=False)
    monkeypatch.setattr(app, "ctk", DummyCTk)
    ui = app.DeepAIApp.__new__(app.DeepAIApp)
    ui.root = object()
    ui.add_to_chat = lambda *a, **k: None
    ui.open_camera_preview()
    output = capsys.readouterr().out
    assert not output, output
    assert windows and windows[0].protocol_handler
    assert windows and windows[0].protocol_handler
    windows[0].protocol_handler()
    assert capture.released


def test_volume_command_parser_supports_status_and_percentages():
    assert SystemAgent._canonical("set volume to 40%", "") == "set volume:40"
    assert SystemAgent._canonical("current volume", "") == "get volume"
    assert SystemAgent._canonical("increase volume", "") == "increase volume"


def test_research_sources_are_real_clickable_http_urls(monkeypatch):
    from agents.research_agent import ResearchAgent
    import ai.ollama
    class FakeDDGS:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def text(self, query, max_results):
            return [
                {"href": "javascript:alert(1)", "title": "unsafe", "body": "skip"},
                {"href": "https://example.org/python", "title": "Python", "body": "A general purpose language."},
            ]
        def extract(self, url, fmt):
            assert fmt == "text_plain"
            return {"url": url, "content": "Python is widely used in data analysis and automation."}
    agent = ResearchAgent.__new__(ResearchAgent)
    agent._ddgs_cls = FakeDDGS
    agent._ddgs_available = True
    class FakeLLM:
        def chat(self, messages, **kwargs):
            assert "UNTRUSTED SOURCE EXCERPT" in messages[1]["content"]
            return "Python is used for automation [1]."
    monkeypatch.setattr(ai.ollama, "get_llm", lambda: FakeLLM())
    result = agent.handle(__import__("core.message", fromlist=["AgentMessage"]).AgentMessage.task(
        from_agent="head_agent", to_agent="research_agent", content="search web for python"))
    text = result.as_text()
    assert "[Python](https://example.org/python)" in text
    assert "javascript:" not in text
    assert "Python is used for automation [1]" in text
    assert "Sources:" in text


def test_research_failure_returns_no_fabricated_urls(monkeypatch):
    from agents.research_agent import ResearchAgent
    class FailingDDGS:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def text(self, *args, **kwargs): raise TimeoutError("search timed out")
    agent = ResearchAgent.__new__(ResearchAgent)
    agent._ddgs_cls = FailingDDGS
    agent._ddgs_available = True
    result = agent.handle(__import__("core.message", fromlist=["AgentMessage"]).AgentMessage.task(
        from_agent="head_agent", to_agent="research_agent", content="search web for Python"))
    assert "Search failed" in result.as_text()
    assert "https://" not in result.as_text()


def test_system_volume_api_supports_get_set_and_mute(monkeypatch):
    class Endpoint:
        def __init__(self): self.level = 0.4; self.muted = 0
        def GetMasterVolumeLevelScalar(self): return self.level
        def GetMute(self): return self.muted
        def SetMasterVolumeLevelScalar(self, value, _): self.level = value
        def SetMute(self, value, _): self.muted = value
    endpoint = Endpoint()
    pycaw = types.ModuleType("pycaw")
    sub = types.ModuleType("pycaw.pycaw")
    sub.AudioUtilities = types.SimpleNamespace(GetSpeakers=lambda: types.SimpleNamespace(EndpointVolume=endpoint))
    sub.IAudioEndpointVolume = types.SimpleNamespace(_iid_=object())
    pycaw.pycaw = sub
    monkeypatch.setitem(__import__("sys").modules, "pycaw", pycaw)
    monkeypatch.setitem(__import__("sys").modules, "pycaw.pycaw", sub)
    assert "40%" in app.SystemControl.volume_control("get")
    assert "70%" in app.SystemControl.volume_control("set:70")
    assert endpoint.level == 0.7
    assert app.SystemControl.volume_control("mute") == "🔇 Muted"
    assert endpoint.muted == 1
    assert "0 to 100" in app.SystemControl.volume_control("set:101")
