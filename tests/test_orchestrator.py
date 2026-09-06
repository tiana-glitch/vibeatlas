import unittest

from career_copilot.agents import Critic, GapAnalyst, JobAnalyst, ResumeMatcher, Writer
from career_copilot.orchestrator import Orchestrator


JOB = "Strong React and TypeScript are required. Python experience is a plus."
RESUME = "Built a React application in TypeScript for internal operations."


class CountingAgent:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = 0

    def run(self, *args):
        self.calls += 1
        return self.delegate.run(*args)


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.agents = {
            "job": CountingAgent(JobAnalyst()),
            "matcher": CountingAgent(ResumeMatcher()),
            "gap": CountingAgent(GapAnalyst()),
            "writer": CountingAgent(Writer()),
            "critic": CountingAgent(Critic()),
        }
        self.orchestrator = Orchestrator(self.agents)

    def test_analysis_has_api_shape_and_stage_events(self):
        result = self.orchestrator.analyze(JOB, RESUME, "Product Engineer")

        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["run_id"].startswith("run_"))
        self.assertEqual(
            set(result["outputs"]),
            {"job_analysis", "resume_match", "gap_analysis", "writer", "critic"},
        )
        event_types = [event["type"] for event in result["events"]]
        self.assertEqual(event_types.count("stage.started"), 5)
        self.assertEqual(event_types.count("stage.completed"), 5)
        self.assertNotIn("stage.failed", event_types)

    def test_retry_executes_only_the_requested_stage(self):
        first = self.orchestrator.analyze(JOB, RESUME)
        before = {name: agent.calls for name, agent in self.agents.items()}

        retried = self.orchestrator.retry_stage(first["run_id"], "gap")

        after = {name: agent.calls for name, agent in self.agents.items()}
        self.assertEqual(after["gap"], before["gap"] + 1)
        for stage in ("job", "matcher", "writer", "critic"):
            self.assertEqual(after[stage], before[stage])
        self.assertEqual(retried["attempts"]["gap"], 2)
        self.assertEqual(
            [event["type"] for event in retried["events"][-3:]],
            ["stage.retry", "stage.started", "stage.completed"],
        )


if __name__ == "__main__":
    unittest.main()
