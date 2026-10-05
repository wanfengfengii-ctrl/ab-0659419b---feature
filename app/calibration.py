"""Exact calibration-graph engine.

Each directed relation states that a reading ``x`` on the ``frm`` instrument
corresponds to a reading ``y`` on the ``to`` instrument via ``y = a*x + b``,
where ``a`` and ``b`` are exact rational numbers and ``a != 0``.

Every coefficient is kept as :class:`fractions.Fraction`, so no floating
point rounding can mask a contradiction: all paths between two instruments
must induce *exactly* the same affine transform, otherwise the request is
rejected as a conflict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterable, Mapping, Sequence


MIN_INSTRUMENTS = 2
MAX_INSTRUMENTS = 50
MAX_RELATIONS = 100
MAX_READINGS = 10_000

# Optional resilience mode: the translation must survive the revocation of
# any single calibration relation.
RESILIENCE_SINGLE_RELATION = "single_relation"


class CalibrationError(Exception):
    """Base class for calibration request failures."""

    code = "calibration_error"
    status = 400


class InvalidRequest(CalibrationError):
    """The request payload failed validation."""

    code = "invalid_request"
    status = 400


class Unreachable(CalibrationError):
    """Source and target lie in different connected components."""

    code = "unreachable"
    status = 404

    def __init__(self, source: str, target: str) -> None:
        super().__init__(f"no calibration path connects {source!r} to {target!r}")
        self.source = source
        self.target = target


class CalibrationConflict(CalibrationError):
    """A cycle induces two different exact transforms."""

    code = "conflict"
    status = 409

    def __init__(self, cycle: Sequence[str], detail: str) -> None:
        super().__init__(detail)
        self.cycle = list(cycle)
        self.detail = detail


class ResilienceNotMet(CalibrationError):
    """Redundancy was requested, but losing one relation breaks the path.

    Carries every relation whose individual removal disconnects source from
    target, sorted by relation id.
    """

    code = "resilience_not_met"
    status = 422

    def __init__(self, critical_relation_ids: Sequence[str]) -> None:
        self.critical_relation_ids = sorted(critical_relation_ids)
        super().__init__(
            "translation has no independent evidence chain without relation(s): "
            + ", ".join(self.critical_relation_ids)
        )


@dataclass(frozen=True)
class Affine:
    """An affine transform ``y = a*x + b`` with exact rational coefficients."""

    a: Fraction
    b: Fraction

    def apply(self, x: Fraction) -> Fraction:
        return self.a * x + self.b

    def compose(self, other: "Affine") -> "Affine":
        """Return ``self ∘ other``: apply ``other`` first, then ``self``."""
        return Affine(self.a * other.a, self.a * other.b + self.b)

    def inverse(self) -> "Affine":
        """Invert the transform (valid because ``a`` is never zero)."""
        inv_a = Fraction(1, 1) / self.a
        return Affine(inv_a, -inv_a * self.b)


IDENTITY = Affine(Fraction(1), Fraction(0))


@dataclass(frozen=True)
class Relation:
    frm: str
    to: str
    a: Fraction
    b: Fraction
    relation_id: str | None = None

    def as_adjacency(self) -> tuple[str, str, Affine]:
        return self.frm, self.to, Affine(self.a, self.b)


def parse_rational(value: object, field_name: str) -> Fraction:
    """Parse an integer or a canonical ``p/q`` string exactly.

    Accepts ``int`` and ``str`` values.  Strings must be either a signed
    integer or a signed fraction ``p/q`` with a non-zero denominator.
    Floating point numbers are rejected on purpose: callers must state
    exact rational values instead of relying on binary rounding.
    """
    if isinstance(value, bool):
        raise InvalidRequest(f"{field_name} must be an integer or p/q string, got boolean")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Fraction):
        return value
    if not isinstance(value, str):
        raise InvalidRequest(
            f"{field_name} must be an integer or p/q string, got {type(value).__name__}"
        )
    text = value.strip()
    if not text:
        raise InvalidRequest(f"{field_name} must be a non-empty rational value")
    if "/" in text:
        num_text, sep, den_text = text.partition("/")
        if not sep or "/" in den_text:
            raise InvalidRequest(f"{field_name}={value!r} is not a valid p/q fraction")
        try:
            numerator = int(num_text, 10)
            denominator = int(den_text, 10)
        except ValueError:
            raise InvalidRequest(f"{field_name}={value!r} is not a valid p/q fraction")
        if denominator == 0:
            raise InvalidRequest(f"{field_name}={value!r} has a zero denominator")
        if denominator < 0:
            numerator, denominator = -numerator, -denominator
        return Fraction(numerator, denominator)
    try:
        return Fraction(int(text, 10))
    except ValueError:
        raise InvalidRequest(f"{field_name}={value!r} is not a valid integer or p/q fraction")


def render_fraction(value: Fraction) -> str:
    """Render a reduced fraction; omit the denominator when it is one."""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _require_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequest(f"{field_name} must be a non-empty string")
    return value.strip()


def _extract_readings(payload: Mapping[str, object]) -> list:
    if "readings" not in payload:
        raise InvalidRequest("field 'readings' is required")
    readings = payload["readings"]
    if not isinstance(readings, list):
        raise InvalidRequest("field 'readings' must be a list")
    if not readings:
        raise InvalidRequest("field 'readings' must contain at least one value")
    if len(readings) > MAX_READINGS:
        raise InvalidRequest(f"field 'readings' may contain at most {MAX_READINGS} values")
    return readings


@dataclass
class TranslationResult:
    source: str
    target: str
    transform: Affine
    readings_in: list[Fraction] = field(default_factory=list)
    readings_out: list[Fraction] = field(default_factory=list)
    resilience_verified: bool = False

    def to_json(self) -> dict:
        body = {
            "source": self.source,
            "target": self.target,
            "coefficients": {
                "a": render_fraction(self.transform.a),
                "b": render_fraction(self.transform.b),
            },
            "readings": [render_fraction(x) for x in self.readings_in],
            "results": [render_fraction(y) for y in self.readings_out],
        }
        if self.resilience_verified:
            body["resilience"] = {"verified": True}
        return body


def translate(payload: Mapping[str, object]) -> TranslationResult:
    """Validate a request and translate readings from source to target.

    Raises :class:`InvalidRequest`, :class:`Unreachable`,
    :class:`CalibrationConflict` or :class:`ResilienceNotMet`.  The answer
    never depends on the order in which relations were entered: every edge is
    checked against every path already derived, and a mismatch rejects the
    whole request.

    When the payload carries ``resilience="single_relation"`` every relation
    must declare a unique non-empty ``relationId``; after the usual exact
    consistency and reachability adjudication, each relation is revoked in
    turn and source/target must stay reachable through the remaining
    relations, otherwise :class:`ResilienceNotMet` is raised with the sorted
    ids of the relations whose loss breaks the translation.  Omitting
    ``resilience`` keeps the original contract untouched.
    """
    if not isinstance(payload, Mapping):
        raise InvalidRequest("request body must be a JSON object")

    relations_raw = payload.get("relations")
    if not isinstance(relations_raw, list):
        raise InvalidRequest("field 'relations' must be a list")
    if not 1 <= len(relations_raw) <= MAX_RELATIONS:
        raise InvalidRequest(
            f"field 'relations' must contain between 1 and {MAX_RELATIONS} relations"
        )

    source = _require_str(payload.get("source"), "field 'source'")
    target = _require_str(payload.get("target"), "field 'target'")
    raw_readings = _extract_readings(payload)

    resilience = payload.get("resilience")
    if resilience is not None and resilience != RESILIENCE_SINGLE_RELATION:
        raise InvalidRequest(
            f"field 'resilience' must be {RESILIENCE_SINGLE_RELATION!r} when provided"
        )
    resilience_requested = resilience is not None

    relations: list[Relation] = []
    vertices: set[str] = set()
    seen_relation_ids: set[str] = set()
    for index, item in enumerate(relations_raw):
        where = f"relations[{index}]"
        if not isinstance(item, Mapping):
            raise InvalidRequest(f"{where} must be an object")
        frm = _require_str(item.get("from"), f"{where}.from")
        to = _require_str(item.get("to"), f"{where}.to")
        if frm == to:
            raise InvalidRequest(f"{where} relates an instrument to itself")
        a = parse_rational(item.get("a"), f"{where}.a")
        if a == 0:
            raise InvalidRequest(f"{where}.a must not be zero")
        b = parse_rational(item.get("b", 0), f"{where}.b")
        relation_id: str | None = None
        if resilience_requested:
            relation_id = _require_str(
                item.get("relationId"),
                f"{where}.relationId (required when resilience is requested)",
            )
            if relation_id in seen_relation_ids:
                raise InvalidRequest(f"duplicate relationId {relation_id!r}")
            seen_relation_ids.add(relation_id)
        relations.append(Relation(frm, to, a, b, relation_id))
        vertices.add(frm)
        vertices.add(to)

    if not MIN_INSTRUMENTS <= len(vertices) <= MAX_INSTRUMENTS:
        raise InvalidRequest(
            f"between {MIN_INSTRUMENTS} and {MAX_INSTRUMENTS} unique instruments "
            f"are required, found {len(vertices)}"
        )
    if source not in vertices:
        raise InvalidRequest(f"source {source!r} does not appear in any relation")
    if target not in vertices:
        raise InvalidRequest(f"target {target!r} does not appear in any relation")

    readings = [parse_rational(v, f"readings[{i}]") for i, v in enumerate(raw_readings)]

    total = _solve_component(relations, source).get(target)
    if total is None:
        raise Unreachable(source, target)

    resilience_verified = False
    if resilience_requested:
        critical = _critical_relations(relations, source, target)
        if critical:
            raise ResilienceNotMet(critical)
        resilience_verified = True

    results = [total.apply(x) for x in readings]
    return TranslationResult(source, target, total, readings, results, resilience_verified)


def _critical_relations(
    relations: Sequence[Relation], source: str, target: str
) -> list[str]:
    """Ids of relations whose individual removal disconnects source/target.

    Each relation is excluded in turn and the remaining graph is probed for
    an (undirected) source->target path.  Consistency of the reduced graphs
    needs no re-check: a subgraph of an already consistent graph cannot
    introduce a contradiction.  Parallel relations between the same pair of
    instruments keep each other reachable, so they count as independent
    evidence under their own ids.
    """
    critical: list[str] = []
    for rel in relations:
        if rel.relation_id is None:  # pragma: no cover - validated upstream
            continue
        reduced = [r for r in relations if r.relation_id != rel.relation_id]
        if not _reachable(reduced, source, target):
            critical.append(rel.relation_id)
    return critical


def _reachable(relations: Iterable[Relation], source: str, target: str) -> bool:
    """Undirected reachability between two instruments."""
    if source == target:
        return True
    adjacency: dict[str, list[str]] = {}
    for rel in relations:
        adjacency.setdefault(rel.frm, []).append(rel.to)
        adjacency.setdefault(rel.to, []).append(rel.frm)
    seen = {source}
    stack = [source]
    while stack:
        node = stack.pop()
        for neighbour in adjacency.get(node, ()):
            if neighbour == target:
                return True
            if neighbour not in seen:
                seen.add(neighbour)
                stack.append(neighbour)
    return False


def _solve_component(relations: Iterable[Relation], source: str) -> dict[str, Affine]:
    """Derive the source->instrument transform for every reachable node.

    Edges are processed in a deterministic traversal, so results do not
    depend on entry order: each edge asserts an equality between two node
    transforms, and any disagreement on a closure edge is a contradictory
    cycle.
    """
    # adjacency: node -> list of (neighbour, transform node->neighbour)
    adjacency: dict[str, list[tuple[str, Affine]]] = {}
    for rel in relations:
        u, v, forward = rel.as_adjacency()
        adjacency.setdefault(u, []).append((v, forward))
        adjacency.setdefault(v, []).append((u, forward.inverse()))

    # Deterministic traversal regardless of the caller's relation ordering.
    for neighbours in adjacency.values():
        neighbours.sort(key=lambda item: item[0])

    transforms: dict[str, Affine] = {source: IDENTITY}
    parent: dict[str, str | None] = {source: None}
    order = [source]
    head = 0
    while head < len(order):
        node = order[head]
        head += 1
        node_tf = transforms[node]
        for neighbour, edge_tf in adjacency.get(node, ()):
            candidate = edge_tf.compose(node_tf)
            existing = transforms.get(neighbour)
            if existing is None:
                transforms[neighbour] = candidate
                parent[neighbour] = node
                order.append(neighbour)
            elif existing != candidate:
                cycle = _cycle_path(parent, node, neighbour)
                detail = (
                    f"contradictory calibration cycle {cycle}: "
                    f"{source}->{neighbour} is both {_fmt(existing)} and {_fmt(candidate)}"
                )
                raise CalibrationConflict(cycle, detail)
    return transforms


def _cycle_path(parent: Mapping[str, str | None], u: str, v: str) -> list[str]:
    """Return the vertices of the fundamental cycle closing with edge v->u.

    It is the spanning-tree path from ``u`` up to their lowest common
    ancestor and back down to ``v``; the contradicting edge itself closes
    the loop (``v`` -> ``u``), so ``u`` is not repeated at the tail.
    """

    def depth_of(node: str) -> int:
        depth = 0
        while parent[node] is not None:
            node = parent[node]  # type: ignore[assignment]
            depth += 1
        return depth

    # Walk both pointers upward in lockstep until they coincide at the LCA.
    pu, pv = u, v
    du, dv = depth_of(u), depth_of(v)
    u_chain: list[str] = []
    v_chain: list[str] = []
    while du > dv:
        u_chain.append(pu)
        pu = parent[pu]  # type: ignore[assignment]
        du -= 1
    while dv > du:
        v_chain.append(pv)
        pv = parent[pv]  # type: ignore[assignment]
        dv -= 1
    while pu != pv:
        u_chain.append(pu)
        v_chain.append(pv)
        pu = parent[pu]  # type: ignore[assignment]
        pv = parent[pv]  # type: ignore[assignment]
    lca = pu

    return u_chain + [lca] + list(reversed(v_chain))


def _fmt(tf: Affine) -> str:
    return f"a={render_fraction(tf.a)}, b={render_fraction(tf.b)}"
