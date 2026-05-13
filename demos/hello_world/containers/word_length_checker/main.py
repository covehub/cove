#!/usr/bin/env python3

from hello_world_common import log, read_secret_word, require_env, write_json, write_text


SERVICE = "word_length_checker"


def main() -> int:
    input_path = require_env("INPUT_PATH")
    result_path = require_env("RESULT_PATH")
    output_path = require_env("OUTPUT_PATH")

    secret_word = read_secret_word(input_path)
    passed = secret_word == secret_word.lower()
    write_json(result_path, {"pass": passed})
    write_text(output_path, secret_word.upper() + "\n")
    log(
        SERVICE,
        f"checked lowercase for {input_path}: pass={passed}; wrote uppercase output to {output_path}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
