from typing_extensions import TypedDict
from typing import Protocol, Annotated, Callable, override

from composer.ui.tool_display import tool_display
from langchain_core.tools import BaseTool
from graphcore.tools.schemas import WithImplementation, WithInjectedState

class ValidationKey(Protocol):
    def to_key(self) -> str:
        ...

    def description(self) -> str:
        ...

def merge_validation(left: dict[str, str], right: dict[str, str]) -> dict[str, str]:
    to_ret = left.copy()
    to_ret.update(right)
    return to_ret

class ValidationState[K: ValidationKey](TypedDict):
    required_validations: list[K]
    validations: Annotated[dict[str, str], merge_validation]

def completion_validations[K: ValidationKey, T: ValidationState](
    t: type[T],
    digester: Callable[[T], str],
    refl: Callable[[T], ValidationState[K]]
) -> tuple[
    Callable[[K, T], dict[str, dict[str, str]]], # stamper
    Callable[[T], str | None], # checker
    list[BaseTool] # introspection
]:
    def stamper(k: K, st: T) -> dict[str, dict[str, str]]:
        return {
            "validations": {
                k.to_key(): digester(st)
            }
        }
    
    def check_completion(st: T) -> str | None:
        st_ = refl(st)
        dig = digester(st)
        errors = []
        for k in st_["required_validations"]:
            if k.to_key() not in st_["validations"]:
                errors.append(f"Missing required completion validation: {k.description()}")
                continue
            if st["validations"][k.to_key()] != dig:
                errors.append(f"Required completion validation {k.description()} is out-of-date, you need to re-run it")
        if errors:
            return f"Completion rejected: {'; '.join(errors)}"
    
    @tool_display("Checking validation status", "Validation gates")
    class ValidationState(WithImplementation[str], WithInjectedState[t]):
        """
        Query the status of the completion validation.

        Returns the current state digest, and status of the required completion gates.

        For completion it is a necessary (but not necessarily suffiient) for all
        required completion gates' stamped digest to equal the current state digest.
        """

        @override
        def run(self) -> str:
            r = self.state["required_validations"]
            dig = digester(self.state)
            to_ret = [f"Current state digest: {dig}", "Required validation gate status:"]
            for k in r:
                if k.to_key() not in self.state["validations"]:
                    to_ret.append(f"{k.description()} -- Not stamped")
                else:
                    to_ret.append(f"{k.description()} -- Stamped digest: {self.state["validations"][k.to_key()]}")
            return "\n".join(to_ret)
    
    return (
        stamper, check_completion, [ValidationState.as_tool("validation_gate_status")]
    )
