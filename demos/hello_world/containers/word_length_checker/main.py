#!/usr/bin/env python3

from hello_world_common import log, read_secret_word, require_env, write_json


SERVICE = "word_length_checker"


def main() -> int:
    alice_input_path = require_env("ALICE_INPUT_PATH")
    bob_input_path = require_env("BOB_INPUT_PATH")
    result_path = require_env("RESULT_PATH")

    combined = read_secret_word(alice_input_path) + read_secret_word(bob_input_path)
    passed = len(combined) == 10
    write_json(result_path, {"pass": passed})
    log(SERVICE, f"checked transformed combined secret word length={len(combined)}: pass={passed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
