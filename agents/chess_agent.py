"""
ChessAgent — wraps the existing ChessGame class.
================================================
Handles:
  * play chess / start game / new game
  * move input (SAN, natural language)
  * coach | hint | best move | why | blunder check | opening | threats
  * save chess / load chess / export pgn / import pgn
  * resign / show board / stop chess

Dialogue routing (Phase 5):
  * start_game claims the next turn so HeadAgent routes the color
    choice directly here.
  * The claim is released the moment color is chosen, OR the moment
    the game ends (checkmate/stalemate/resign), OR when 'stop chess'
    is called. This ensures subsequent unrelated messages go through
    the normal planner.
"""
from __future__ import annotations

import glob
import os
import re
from typing import Optional

from agents.base_agent import BaseAgent
from core.message import AgentMessage, MessageType


class ChessAgent(BaseAgent):
    name = "chess_agent"

    def __init__(self) -> None:
        self._local_game = None

    # ------------------------------------------------------------------
    def _game(self):
        live = self.get_service("chess_game")
        if live is not None:
            return live
        if self._local_game is None:
            self._local_game = self._build_local_game()
        return self._local_game

    def _build_local_game(self):
        from core.agent_manager import get_app_module
        mod = get_app_module()
        return mod.ChessGame()

    # ------------------------------------------------------------------
    def _release_claim(self) -> None:
        try:
            from core.context import get_session
            get_session().release_dialogue()
        except Exception:
            pass

    def _claim_turn(self, note: str) -> None:
        try:
            from core.context import get_session
            get_session().claim_dialogue(self.name, note=note)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def handle(self, msg: AgentMessage) -> AgentMessage:
        text = msg.content if isinstance(msg.content, str) else str(
            msg.content.get("text", "")
        )
        cmd = text.strip().lower()
        action = (msg.context.get("action") or "").strip().lower()

        verb = self._canonical(cmd, action)

        self.publish_status(
            f"chess: {verb or action or cmd or 'unknown'}", msg
        )

        try:
            game = self._game()
        except Exception as e:
            return self.fail(str(e), msg)

        # ---- explicit stop ----
        if verb == "stop_chess":
            # Abandon the current game and release any dialogue claim.
            if game.is_active():
                game.resign()
            game.game_active = False
            try:
                game.waiting_for_color = False
            except Exception:
                pass
            self._release_claim()
            return self.respond("♟️ Chess stopped. What would you like to do?", msg)

        # ---- game lifecycle: start a new game ----
        if verb == "start_game":
            self._claim_turn("awaiting chess color choice")
            return self.respond(game.start_game_with_color_prompt(), msg)

        # ---- color selection while waiting ----
        if getattr(game, "waiting_for_color", False) and cmd in (
            "white", "w", "1", "black", "b", "2", "random", "r", "3"
        ):
            reply = game.set_color(cmd)
            # Color chosen — release so the next turn goes to the planner.
            self._release_claim()
            return self.respond(reply, msg)

        # ---- save / load / pgn ----
        if verb == "save_chess":
            fp, fn = game.save_game()
            return self.respond(f"💾 Saved: {fn}", msg)

        if verb == "load_chess":
            parts = cmd.split()
            filename = parts[2] if len(parts) > 2 else None
            if filename:
                ok, msg_out = game.load_game(filename)
            else:
                saves = glob.glob("chess_saves/*.json")
                if saves:
                    latest = max(saves, key=os.path.getmtime)
                    ok, msg_out = game.load_game(os.path.basename(latest))
                else:
                    ok, msg_out = False, "No saved chess games found."
            return self.respond(msg_out, msg)

        if verb == "export_pgn":
            fp, pgn = game.export_pgn()
            return self.respond(
                f"📋 Exported PGN to {os.path.basename(fp)}", msg
            )

        if verb == "import_pgn":
            pgn_text = text[len("import pgn"):].strip()
            ok, msg_out = game.import_pgn(pgn_text)
            return self.respond(msg_out, msg)

        # ---- in-game actions require an active game ----
        if not game.is_active():
            # Release any stale claim if we've fallen through here.
            self._release_claim()
            if verb in ("coach", "hint", "best_move", "why",
                        "show_board", "rate_move", "blunder_check",
                        "opening", "explain_position", "show_threats"):
                return self.respond(
                    "No active chess game. Say 'play chess' to start.", msg
                )
            return self.fail(
                f"ChessAgent: no active game and unrecognized command "
                f"'{cmd}' / action '{action}'.",
                msg,
            )

        if verb == "coach":
            return self.respond(game.coach_analysis(), msg)
        if verb == "hint":
            return self.respond(game.get_hint(), msg)
        if verb == "rate_move":
            return self.respond(game.rate_last_move(), msg)
        if verb == "blunder_check":
            return self.respond(game.check_blunders(), msg)
        if verb == "opening":
            return self.respond(
                game.detect_opening() or "Unknown opening", msg
            )
        if verb == "explain_position":
            return self.respond(game.explain_position(), msg)
        if verb == "show_threats":
            return self.respond(game.show_threats_command(), msg)
        if verb == "best_move":
            best = game.get_best_move(15)
            if best:
                try:
                    return self.respond(
                        f"💡 Best: {game.board.san(best)}", msg
                    )
                except Exception:
                    pass
            return self.respond("No best move found.", msg)
        if verb == "why":
            best = game.get_best_move(12)
            if best:
                try:
                    san = game.board.san(best)
                    return self.respond(
                        f"💡 Why {san}?\n{game.explain_move(best)}", msg
                    )
                except Exception:
                    pass
            return self.respond("Could not explain the position.", msg)
        if verb == "show_board":
            return self.respond(game.get_board_display(), msg)
        if verb == "resign":
            reply = game.resign()
            self._release_claim()
            return self.respond(reply, msg)

        # ---- treat as a move ----
        if verb == "move" or self._looks_like_move(cmd):
            self.publish_status(f"chess: playing move {cmd}", msg)
            reply = game.make_move(cmd)
            # If the game ended, release the dialogue claim.
            if not game.is_active():
                self._release_claim()
            return self.respond(reply, msg)

        # Unknown command — release the claim and fail so HeadAgent can
        # either retry or fall back.
        self._release_claim()
        return self.fail(
            f"ChessAgent: unrecognized command '{cmd}' / "
            f"action '{action}'.",
            msg,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _canonical(cmd: str, action: str) -> str:
        cmd = (cmd or "").strip().lower()
        action = (action or "").strip().lower()
        hay = f"{cmd} || {action}"

        exact = {
            # start
            "play chess":      "start_game",
            "start chess":     "start_game",
            "new game":        "start_game",
            "new chess":       "start_game",
            "start_game":      "start_game",
            "play":            "start_game",
            # stop
            "stop chess":      "stop_chess",
            "quit chess":      "stop_chess",
            "end chess":       "stop_chess",
            "exit chess":      "stop_chess",
            "stop_chess":      "stop_chess",
            "quit_chess":      "stop_chess",
            # save / load / pgn
            "save chess":      "save_chess",
            "save_game":       "save_chess",
            "load chess":      "load_chess",
            "load_game":       "load_chess",
            "export pgn":      "export_pgn",
            "export_pgn":      "export_pgn",
            "import pgn":      "import_pgn",
            "import_pgn":      "import_pgn",
            # coaching
            "coach":           "coach",
            "hint":            "hint",
            "best move":       "best_move",
            "best_move":       "best_move",
            "why":             "why",
            "rate move":       "rate_move",
            "rate_move":       "rate_move",
            "blunder check":   "blunder_check",
            "blunder_check":   "blunder_check",
            "opening":         "opening",
            "explain position":"explain_position",
            "explain_position":"explain_position",
            "show threats":    "show_threats",
            "show_threats":    "show_threats",
            "show board":      "show_board",
            "show_board":      "show_board",
            "resign":          "resign",
        }
        if cmd in exact:
            return exact[cmd]
        if action in exact:
            return exact[action]

        # substring heuristics
        if any(k in hay for k in (
            "stop_chess", "stop chess", "quit_chess", "quit chess",
            "end chess", "end_chess", "exit chess", "exit_chess",
        )):
            return "stop_chess"
        if any(k in hay for k in (
            "play_chess", "play chess", "start_chess", "start chess",
            "start_game", "new_game", "new game", "start a game",
            "begin chess", "begin a game",
        )):
            return "start_game"
        if any(k in hay for k in ("save_chess", "save chess", "save_game")):
            return "save_chess"
        if any(k in hay for k in ("load_chess", "load chess", "load_game")):
            return "load_chess"
        if "export" in hay and "pgn" in hay:
            return "export_pgn"
        if "import" in hay and "pgn" in hay:
            return "import_pgn"
        if "blunder" in hay:
            return "blunder_check"
        if "threat" in hay:
            return "show_threats"
        if "rate" in hay and "move" in hay:
            return "rate_move"
        if "coach" in hay:
            return "coach"
        if "hint" in hay:
            return "hint"
        if "best" in hay and "move" in hay:
            return "best_move"
        if "explain" in hay and "position" in hay:
            return "explain_position"
        if "opening" in hay:
            return "opening"
        if "show" in hay and "board" in hay:
            return "show_board"
        if "resign" in hay:
            return "resign"
        if "why" in hay:
            return "why"

        return ""

    @staticmethod
    def _looks_like_move(cmd: str) -> bool:
        patterns = [
            r"^[a-h][1-8]$",
            r"^[nbrqk][a-h]?[1-8]?x?[a-h][1-8]$",
            r"^o-o(-o)?$",
        ]
        return any(re.match(p, cmd) for p in patterns) or (
            len(cmd) <= 5 and cmd[:1] in "abcdefghnbrqk"
        )