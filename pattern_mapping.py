"""Read the explicit combinational capture format emitted by this ATPG pipeline.

STIL group order describes the bit strings; Verilog declaration order does not.
This is deliberately not a general STIL interpreter. Unsupported constructs,
unknown bits, missing signals and mismatched pattern indices are errors.
"""
from dataclasses import dataclass
import re


MAPPING_VERSION = "stil-signal-groups-v1"
_QUOTED = r'"(?:\\.|[^"\\])*"'


def _unquote(token):
    # STIL quoted names escape quotes/backslashes, not Python/JSON sequences.
    return re.sub(r'\\([\\"])', r'\1', token[1:-1])


@dataclass(frozen=True)
class PatternMapping:
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    patterns: dict[int, tuple[str, str]]

    def vectors(self, index, input_bits, output_bits, input_nets, output_nets):
        source = self.patterns.get(int(index))
        if source != (input_bits, output_bits):
            raise ValueError(f"Pattern {index}: CSV bits differ from STIL capture: {source}")
        mapped = []
        for kind, bits, group, ports in (
            ("PI", input_bits, self.inputs, input_nets),
            ("PO", output_bits, self.outputs, output_nets),
        ):
            if len(bits) != len(group) or set(bits) - {"0", "1"}:
                raise ValueError(f"Pattern {index}: invalid {kind} width or nonbinary bits")
            if len(ports) != len(set(ports)) or set(ports) != set(group):
                raise ValueError(f"{kind} STIL/Verilog ports differ: "
                                 f"missing={sorted(set(ports)-set(group))}, "
                                 f"extra={sorted(set(group)-set(ports))}")
            values = dict(zip(group, map(int, bits), strict=True))
            # Preserve the simulator's canonical serialization order AFTER mapping.
            mapped.append({port: values[port] for port in ports})
        return tuple(mapped)


def read_pattern_mapping(text):
    # Preserve quoted names while removing comments and annotation bodies.
    text = re.sub(_QUOTED + r'|//[^\n]*|/\*.*?\*/|Ann\s*\{\*.*?\*\}',
                  lambda m: m[0] if m[0].startswith('"') else " ", text, flags=re.S)
    groups = re.findall(r'\bSignalGroups\s*\{([^{}]*)\}', text, re.S)
    if len(groups) != 1:
        raise ValueError("Expected one explicit, unnamed SignalGroups block")
    def group(name):
        expressions = re.findall(r'"' + name + r'"\s*=\s*\x27([^\x27]*)\x27\s*;', groups[0])
        if len(expressions) != 1:
            raise ValueError(f"Expected exactly one {name} group")
        expr = expressions[0].strip()
        if not re.fullmatch(_QUOTED + r'(?:\s*\+\s*' + _QUOTED + r')*', expr):
            raise ValueError(f"Unsupported {name} group expression: {expr}")
        names = tuple(_unquote(x) for x in re.findall(_QUOTED, expr))
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate signal in {name}")
        return names
    inputs, outputs = group("_pi"), group("_po")
    labels = re.findall(r'"pattern\s+(\d+)"\s*:', text)
    captures = re.findall(r'"pattern\s+(\d+)"\s*:\s*Call\s+"capture"\s*\{([^{}]*)\}', text, re.S)
    if not labels or len(labels) != len(captures):
        raise ValueError("Missing or unsupported numbered capture patterns")
    patterns = {}
    for label, body in captures:
        index = int(label)
        if index in patterns:
            raise ValueError(f"Duplicate pattern index {index}")
        match = re.fullmatch(r'\s*"_pi"\s*=\s*([01\s]+);\s*"_po"\s*=\s*([HL\s]+);\s*', body)
        if not match:
            raise ValueError(f"Pattern {index}: unsupported capture or nonbinary waveform")
        pi, po = (re.sub(r'\s+', '', bits) for bits in match.groups())
        po = po.translate(str.maketrans("HL", "10"))
        if len(pi) != len(inputs) or len(po) != len(outputs):
            raise ValueError(f"Pattern {index}: STIL vector/group width mismatch")
        patterns[index] = (pi, po)
    return PatternMapping(inputs, outputs, patterns)
