#!/usr/bin/env python3
"""Run PQ from a terminal. Needs LLM_PROVIDER and its key in the environment.

    python cli.py ask "what is a Kubernetes operator?"   # one question, one answer
    python cli.py chat                                   # a conversation; follow-ups keep the context
    python cli.py teams-test                             # send a test card to TEAMS_REPLY_WEBHOOK_URL

The terminal is for the administrators: the boundaries that apply in Teams are off here.
"""
import argparse

import agent
import teams


def _say(history, question):
    """Print the answer while the model writes it."""
    for piece in agent.chat_stream(history, question):
        print(piece, end="", flush=True)
    print()


def main():
    parser = argparse.ArgumentParser(description="PQ - a technical teacher for any technology")
    sub = parser.add_subparsers(dest="command")

    ask_p = sub.add_parser("ask", help="Ask one question and print the explanation")
    ask_p.add_argument("question", nargs="+", help="The question, in plain words")

    sub.add_parser("chat", help="Ask questions in a conversation (empty line or Ctrl+C to stop)")
    sub.add_parser("teams-test", help="Send a test card to the Teams reply webhook")

    args = parser.parse_args()

    if args.command == "ask":
        _say([], " ".join(args.question))

    elif args.command == "chat":
        history = []
        print("PQ - ask a technical question. An empty line stops.")
        while True:
            try:
                question = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not question:
                break
            print()
            _say(history, question)

    elif args.command == "teams-test":
        print(teams.post_test() or "TEAMS_REPLY_WEBHOOK_URL is not set.")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
