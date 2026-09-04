from typing import Callable

def prompt_input(prompt_str: str, filter: Callable[[str], str | None] | None = None) -> str:
    l = input(prompt_str + " (double newlines ends): ")
    buffer = ""
    num_consecutive_blank = 0
    while True:
        x = l.strip()
        buffer += x + "\n"
        if x == "":
            num_consecutive_blank += 1
        else:
            num_consecutive_blank = 0
        if num_consecutive_blank == 2:
            break
        l = input("> ")
    if filter is None:
        return buffer
    filter_res = filter(buffer)
    if filter_res is None:
        return buffer
    return prompt_input(prompt_str, filter)
