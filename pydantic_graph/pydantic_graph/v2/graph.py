from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, assert_never, cast, get_args, get_origin

from typing_extensions import Literal, TypeVar

from pydantic_graph.v2.decision import Decision
from pydantic_graph.v2.id_types import ForkStack, ForkStackItem, GraphRunId, JoinId, NodeId, NodeRunId, TaskId
from pydantic_graph.v2.join import Join, Reducer
from pydantic_graph.v2.node import (
    EndNode,
    Fork,
    StartNode,
)
from pydantic_graph.v2.node_types import AnyNode
from pydantic_graph.v2.parent_forks import ParentFork
from pydantic_graph.v2.paths import BroadcastMarker, DestinationMarker, LabelMarker, Path, SpreadMarker, TransformMarker
from pydantic_graph.v2.step import Step, StepContext
from pydantic_graph.v2.util import unpack_type_expression

if TYPE_CHECKING:
    from pydantic_graph.v2.mermaid import StateDiagramDirection


StateT = TypeVar('StateT', infer_variance=True)
InputT = TypeVar('InputT', infer_variance=True)
OutputT = TypeVar('OutputT', infer_variance=True)


@dataclass
class EndMarker(Generic[OutputT]):
    """An end marker."""

    value: OutputT


@dataclass
class JoinItem:
    """A join item."""

    join_id: JoinId
    inputs: Any
    fork_stack: ForkStack


@dataclass(repr=False)
class Graph(Generic[StateT, InputT, OutputT]):
    """A graph."""

    state_type: type[StateT]
    input_type: type[InputT]
    output_type: type[OutputT]

    nodes: dict[NodeId, AnyNode]
    edges_by_source: dict[NodeId, list[Path]]
    parent_forks: dict[JoinId, ParentFork[NodeId]]

    def get_parent_fork(self, join_id: JoinId) -> ParentFork[NodeId]:
        result = self.parent_forks.get(join_id)
        if result is None:
            raise RuntimeError(f'Node {join_id} is not a join node or did not have a dominating fork (this is a bug)')
        return result

    async def run(self, state: StateT, inputs: InputT) -> OutputT:
        async with self.iter(state, inputs) as graph_run:
            # Note: This would probably be better using `async for _ in graph_run`, but this tests the `next` method,
            # which I'm less confident will be implemented correctly if not used on the critical path. We can change it
            # once we have tests, etc.
            event: Any = None
            while True:
                try:
                    event = await graph_run.next(event)
                except StopAsyncIteration:
                    assert isinstance(event, EndMarker), 'Graph run should end with an EndMarker.'
                    return cast(EndMarker[OutputT], event).value

    @asynccontextmanager
    async def iter(self, state: StateT, inputs: InputT) -> AsyncIterator[GraphRun[StateT, OutputT]]:
        yield GraphRun[StateT, OutputT](
            graph=self,
            state=state,
            inputs=inputs,
        )

    def render(self, *, title: str | None = None, direction: StateDiagramDirection | None = None) -> str:
        from pydantic_graph.v2.mermaid import build_mermaid_graph

        return build_mermaid_graph(self).render(title=title, direction=direction)

    def __repr__(self):
        return self.render()


@dataclass
class GraphTask:
    """A graph task."""

    # With our current BaseNode thing, next_node_id and next_node_inputs are merged into `next_node` itself
    node_id: NodeId
    inputs: Any
    fork_stack: ForkStack
    """
    Stack of forks that have been entered; used so that the GraphRunner can decide when to proceed through joins
    """

    task_id: TaskId = field(default_factory=lambda: TaskId(str(uuid.uuid4())))


class GraphRun(Generic[StateT, OutputT]):
    """A graph run."""

    def __init__(
        self,
        graph: Graph[StateT, InputT, OutputT],
        state: StateT,
        inputs: InputT,
    ):
        self._graph = graph
        self._state = state
        self._active_reducers: dict[tuple[JoinId, NodeRunId], Reducer[Any, Any, Any]] = {}

        self._next: EndMarker[OutputT] | JoinItem | Sequence[GraphTask] | None = None

        run_id = GraphRunId(str(uuid.uuid4()))
        initial_fork_stack: ForkStack = (ForkStackItem(StartNode.id, NodeRunId(run_id), 0),)
        self._first_task = GraphTask(node_id=StartNode.id, inputs=inputs, fork_stack=initial_fork_stack)
        self._iterator = self._iter_graph()

    def __aiter__(self) -> AsyncIterator[EndMarker[OutputT] | JoinItem | Sequence[GraphTask]]:
        return self

    async def __anext__(self) -> EndMarker[OutputT] | JoinItem | Sequence[GraphTask]:
        if self._next is None:
            self._next = await self._iterator.__anext__()
        else:
            self._next = await self._iterator.asend(self._next)
        return self._next

    async def next(
        self, value: EndMarker[OutputT] | JoinItem | Sequence[GraphTask] | None = None
    ) -> EndMarker[OutputT] | JoinItem | Sequence[GraphTask]:
        """Allows for sending a value to the iterator, which is useful for resuming the iteration."""
        if value is not None:
            self._next = value
        return await self.__anext__()

    async def _iter_graph(
        self,
    ) -> AsyncGenerator[
        EndMarker[OutputT] | JoinItem | Sequence[GraphTask], EndMarker[OutputT] | JoinItem | Sequence[GraphTask]
    ]:
        tasks_by_id: dict[TaskId, GraphTask] = {}
        pending: set[asyncio.Task[EndMarker[OutputT] | JoinItem | Sequence[GraphTask]]] = set()

        def _start_task(t_: GraphTask) -> None:
            """Helper function to start a new task while doing all necessary tracking."""
            tasks_by_id[t_.task_id] = t_
            pending.add(asyncio.create_task(self._handle_task(t_), name=t_.task_id))

        _start_task(self._first_task)

        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                result = task.result()
                source_task = tasks_by_id.pop(TaskId(task.get_name()))
                result = yield result
                if isinstance(result, EndMarker):
                    for t in pending:
                        t.cancel()
                    return

                if isinstance(result, JoinItem):
                    parent_fork_id = self._graph.get_parent_fork(result.join_id).fork_id
                    fork_run_id = [x.node_run_id for x in result.fork_stack[::-1] if x.fork_id == parent_fork_id][0]
                    reducer = self._active_reducers.get((result.join_id, fork_run_id))
                    if reducer is None:
                        join_node = self._graph.nodes[result.join_id]
                        assert isinstance(join_node, Join)
                        reducer = join_node.create_reducer(StepContext(None, result.inputs))
                        self._active_reducers[(result.join_id, fork_run_id)] = reducer
                    else:
                        reducer.reduce(StepContext(None, result.inputs))
                else:
                    for new_task in result:
                        _start_task(new_task)

                for join_id, fork_run_id, fork_stack in self._get_completed_fork_runs(
                    source_task, tasks_by_id.values()
                ):
                    reducer = self._active_reducers.pop((join_id, fork_run_id))

                    output = reducer.finalize(StepContext(None, None))
                    join_node = self._graph.nodes[join_id]
                    assert isinstance(join_node, Join)  # We could drop this but if it fails it means there is a bug.
                    new_tasks = self._handle_edges(join_node, output, fork_stack)
                    for new_task in new_tasks:
                        _start_task(new_task)

        raise RuntimeError(
            'Graph run completed, but no result was produced. This is either a bug in the graph or a bug in the graph runner.'
        )

    async def _handle_task(
        self,
        task: GraphTask,
    ) -> Sequence[GraphTask] | JoinItem | EndMarker[OutputT]:
        state = self._state
        node_id = task.node_id
        inputs = task.inputs
        fork_stack = task.fork_stack

        node = self._graph.nodes[node_id]
        if isinstance(node, (StartNode, Fork)):
            return self._handle_edges(node, inputs, fork_stack)
        elif isinstance(node, Step):
            step_context = StepContext[StateT, Any](state, inputs)
            output = await node.call(step_context)
            return self._handle_edges(node, output, fork_stack)
        elif isinstance(node, Join):
            return JoinItem(node_id, inputs, fork_stack)
        elif isinstance(node, Decision):
            return self._handle_decision(node, inputs, fork_stack)
        elif isinstance(node, EndNode):
            return EndMarker(inputs)
        else:
            assert_never(node)

    def _handle_decision(
        self, decision: Decision[StateT, Any], inputs: Any, fork_stack: ForkStack
    ) -> Sequence[GraphTask]:
        for branch in decision.branches:
            match_tester = branch.matches
            if match_tester is not None:
                inputs_match = match_tester(inputs)
            else:
                branch_source = unpack_type_expression(branch.source)

                if branch_source in {Any, object}:
                    inputs_match = True
                elif get_origin(branch_source) is Literal:
                    inputs_match = inputs in get_args(branch_source)
                else:
                    try:
                        inputs_match = isinstance(inputs, branch_source)
                    except TypeError as e:
                        raise RuntimeError(f'Decision branch source {branch_source} is not a valid type.') from e

            if inputs_match:
                return self._handle_path(branch.path, inputs, fork_stack)

        raise RuntimeError(f'No branch matched inputs {inputs} for decision node {decision}.')

    def _get_completed_fork_runs(
        self,
        t: GraphTask,
        active_tasks: Iterable[GraphTask],
    ) -> list[tuple[JoinId, NodeRunId, ForkStack]]:
        completed_fork_runs: list[tuple[JoinId, NodeRunId, ForkStack]] = []

        fork_run_indices = {fsi.node_run_id: i for i, fsi in enumerate(t.fork_stack)}
        for join_id, fork_run_id in self._active_reducers.keys():
            fork_run_index = fork_run_indices.get(fork_run_id)
            if fork_run_index is None:
                continue  # The fork_run_id is not in the current task's fork stack, so this task didn't complete it.

            new_fork_stack = t.fork_stack[:fork_run_index]
            # This reducer _may_ now be ready to finalize:
            if self._is_fork_run_completed(active_tasks, join_id, fork_run_id):
                completed_fork_runs.append((join_id, fork_run_id, new_fork_stack))

        return completed_fork_runs

    def _handle_path(self, path: Path, inputs: Any, fork_stack: ForkStack) -> Sequence[GraphTask]:
        if not path.items:
            return []

        item = path.items[0]
        if isinstance(item, DestinationMarker):
            return [GraphTask(item.destination_id, inputs, fork_stack)]
        elif isinstance(item, SpreadMarker):
            node_run_id = NodeRunId(str(uuid.uuid4()))
            return [
                GraphTask(
                    item.fork_id, input_item, fork_stack + (ForkStackItem(item.fork_id, node_run_id, thread_index),)
                )
                for thread_index, input_item in enumerate(inputs)
            ]
        elif isinstance(item, BroadcastMarker):
            return [GraphTask(item.fork_id, inputs, fork_stack)]
        elif isinstance(item, TransformMarker):
            inputs = item.transform(StepContext(self._state, inputs))
            return self._handle_path(path.next_path, inputs, fork_stack)
        elif isinstance(item, LabelMarker):
            return self._handle_path(path.next_path, inputs, fork_stack)
        else:
            assert_never(item)

    def _handle_edges(self, node: AnyNode, inputs: Any, fork_stack: ForkStack) -> Sequence[GraphTask]:
        edges = self._graph.edges_by_source.get(node.id, [])
        assert len(edges) == 1 or isinstance(node, Fork)  # this should have already been ensured during graph building

        new_tasks: list[GraphTask] = []
        for path in edges:
            new_tasks.extend(self._handle_path(path, inputs, fork_stack))
        return new_tasks

    def _is_fork_run_completed(self, tasks: Iterable[GraphTask], join_id: JoinId, fork_run_id: NodeRunId) -> bool:
        # Check if any of the tasks in the graph have this fork_run_id in their fork_stack
        # If this is the case, then the fork run is not yet completed
        parent_fork = self._graph.get_parent_fork(join_id)
        for t in tasks:
            if fork_run_id in {x.node_run_id for x in t.fork_stack}:
                if t.node_id in parent_fork.intermediate_nodes:
                    return False
        return True
