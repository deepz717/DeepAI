"""Offline regression checks for DeepAI's chat routing and context handling."""
from pathlib import Path
from types import SimpleNamespace

from agents.head_agent import HeadAgent
from core.context_resolver import is_conversational


class _ChatModel:
    chat_model = "test-chat"
    text_model = "test-text"

    def __init__(self, reply="Hello! I'm DeepAI. How can I help?"):
        self.reply = reply
        self.messages = None
        self.kwargs = None

    def chat(self, messages, **kwargs):
        self.messages = messages
        self.kwargs = kwargs
        return self.reply


def _head(monkeypatch, reply="Hello! I'm DeepAI. How can I help?"):
    model = _ChatModel(reply)
    head = HeadAgent.__new__(HeadAgent)
    head._llm = model
    monkeypatch.setattr(_ChatModel, "is_alive", lambda self: True, raising=False)
    return head, model


def test_chat_uses_deepai_identity_and_natural_greeting(monkeypatch):
    head, model = _head(monkeypatch)
    answer = head._direct_answer("Hello")
    assert answer == "Hello!"
    assert len(answer.split()) <= 10
    assert model.messages[0]["role"] == "system"
    assert "You are DeepAI" in model.messages[0]["content"]
    assert model.messages[-1] == {"role": "user", "content": "Hello"}
    assert model.kwargs["max_tokens"] == 32
    assert model.kwargs["stop"] == ["\n\n\n", "\nUser:", "\nAssistant:"]
    assert "who created, owns, or developed you" in model.messages[0]["content"]


def test_how_are_you_remains_a_natural_model_response(monkeypatch):
    head, _ = _head(monkeypatch, "I'm doing well. I can help with anything.")
    assert head._direct_answer("How are you?") == "I'm doing well."


def test_identity_questions_do_not_invent_creator_or_company():
    expected = {
        "Who are you?": ("DeepAI", "Ollama"),
        "Who created you?": ("verified information", "Ollama"),
        "Are you ChatGPT?": ("No", "Ollama"),
        "What can you do?": ("code", "optional software"),
    }
    for question, required in expected.items():
        answer = HeadAgent._try_identity(question)
        assert answer
        assert all(word.casefold() in answer.casefold() for word in required)
        assert "Microsoft" not in answer


def test_generated_self_identity_claims_are_removed_but_other_facts_remain():
    text = "DeepAI was designed by Microsoft. I can help with Python."
    assert HeadAgent._sanitize_assistant_identity_claims(text) == "I can help with Python."
    assert HeadAgent._sanitize_assistant_identity_claims(
        "Python was created by Guido van Rossum.") == "Python was created by Guido van Rossum."


def test_legacy_chat_prompt_and_welcome_do_not_attribute_a_creator():
    source = Path(__file__).resolve().parents[1].joinpath("app.py").read_text(encoding="utf-8")
    assert "created by {CREATOR_NAME}" not in source
    assert "Created by {CREATOR_NAME}" not in source
    assert "Do not guess or claim who created, owns," in source


def test_history_parser_handles_app_history_format():
    history = (
        "## Recent Conversation History:\n"
        "User: My name is Deepanshu.\n"
        "Assistant: Nice to meet you, Deepanshu.\n"
    )
    assert HeadAgent._history_messages(history) == [
        {"role": "user", "content": "My name is Deepanshu."},
        {"role": "assistant", "content": "Nice to meet you, Deepanshu."},
    ]


def test_direct_answer_includes_prior_history_and_avoids_duplicate_current_turn(monkeypatch):
    from core import memory as memory_module

    fake_memory = SimpleNamespace(
        recent_context=lambda n=12: (
            "## Recent Conversation History:\n"
            "User: My name is Deepanshu.\n"
            "Assistant: Nice to meet you.\n"
            "User: What is my name?\n"
        ),
        long_term_context_string=lambda: "",
    )
    monkeypatch.setattr(memory_module, "get_memory", lambda: fake_memory)
    head, model = _head(monkeypatch, "Your name is Deepanshu.")
    assert head._direct_answer("What is my name?") == "Your name is Deepanshu."
    assert model.messages[1:3] == [
        {"role": "user", "content": "My name is Deepanshu."},
        {"role": "assistant", "content": "Nice to meet you."},
    ]
    assert model.messages[-1] == {"role": "user", "content": "What is my name?"}
    assert sum(m == {"role": "user", "content": "What is my name?"} for m in model.messages) == 1
    assert model.kwargs["max_tokens"] == 128


def test_empty_model_answer_is_user_friendly(monkeypatch):
    head, _ = _head(monkeypatch, "")
    assert "couldn’t generate a reply" in head._direct_answer("Hi")


def test_qwen3_final_answer_retry_handles_thinking_only_response(monkeypatch):
    from ai.ollama import LocalLLM

    llm = LocalLLM(host="http://127.0.0.1:11434", text_model="qwen3:8b")
    monkeypatch.setattr(llm, "is_alive", lambda refresh=False: True)

    class Response:
        status_code = 200
        text = ""
        def __init__(self, message): self.message = message
        def json(self): return {"message": self.message}

    calls = []
    def post(url, json, timeout):
        calls.append(json)
        if len(calls) == 1:
            return Response({"role": "assistant", "content": "", "thinking": "private reasoning"})
        return Response({"role": "assistant", "content": "Python is a programming language."})

    monkeypatch.setattr(llm._session, "post", post)
    result = llm.chat([{"role": "user", "content": "Explain Python."}], max_tokens=48)
    assert result == "Python is a programming language."
    assert len(calls) == 2
    assert calls[1]["options"]["num_predict"] >= 256
    assert "private reasoning" not in result


def test_qwen3_retry_failure_returns_empty_without_exposing_reasoning(monkeypatch):
    from ai.ollama import LocalLLM

    llm = LocalLLM(host="http://127.0.0.1:11434", text_model="qwen3:8b")
    monkeypatch.setattr(llm, "is_alive", lambda refresh=False: True)
    class Response:
        status_code = 200
        text = ""
        def json(self): return {"message": {"content": "", "thinking": "secret chain"}}
    monkeypatch.setattr(llm._session, "post", lambda *a, **k: Response())
    assert llm.chat([{"role": "user", "content": "Hello"}]) == ""


def test_chat_rejects_empty_input_and_classifies_greetings():
    head = HeadAgent.__new__(HeadAgent)
    assert head.run("   ") == "Please enter a message and I’ll help."
    assert is_conversational("Hello")
    assert is_conversational("Good morning")


def test_personal_fact_capture_is_narrow_and_recall_uses_memory(monkeypatch):
    from core.agent_manager import register_service

    class _FakeMemory:
        def __init__(self):
            self.values = {}

        def remember(self, key, value):
            self.values[key] = value

        def recall(self, key):
            return self.values.get(key)

    fake = _FakeMemory()
    register_service("app_module", SimpleNamespace(ai_memory=fake))
    assert HeadAgent._personal_fact("My name is Deepanshu.") == ("name", "Deepanshu")
    assert HeadAgent._personal_fact("I met Deepanshu yesterday.") is None
    HeadAgent._remember_personal_fact("name", "Deepanshu")
    assert HeadAgent._personal_recall("What is my name?") == "You told me your name is Deepanshu."


def test_personal_fact_returns_saved_confirmation_without_redundant_agent_route(monkeypatch):
    stored = []
    monkeypatch.setattr(
        HeadAgent, "_remember_personal_fact",
        staticmethod(lambda key, value: stored.append((key, value))),
    )
    head = HeadAgent.__new__(HeadAgent)
    assert head._run_request("My name is Deepanshu.") == "I’ll remember that your name is Deepanshu."
    assert stored == [("name", "Deepanshu")]
