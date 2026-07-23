from dataclasses import dataclass, field

class NoSuchElementError(RuntimeError):
    pass


@dataclass
class ListIter[T]:
    l: list[T]
    ind: int = field(default=0)

    def has_next(self) -> bool:
        return self.ind < len(self.l)

    def peek(self) -> T:
        if not self.has_next():
            raise NoSuchElementError("Invalid state, no more elements")
        return self.l[self.ind]

    def next(self) -> T:
        if not self.has_next():
            raise NoSuchElementError("Invalid state, no more elements")
        to_ret = self.l[self.ind]
        self.ind += 1
        return to_ret
