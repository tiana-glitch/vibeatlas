import unittest

from career_copilot.agents import Critic, GapAnalyst, JobAnalyst, ResumeMatcher, Writer
from career_copilot.models import DraftEdit, WriterOutput


JOB = """Product Engineer
We need strong React and TypeScript fundamentals, comfort with APIs and data modeling.
Experience with Python or LLM products is a plus."""

RESUME = """Alex Liu, Product Engineer
Led a React and TypeScript redesign used by 18k teams.
Built APIs for a workflow product and maintained PostgreSQL data models."""

CHINESE_JOB = """产品工程师
需要扎实的 React 与 TypeScript 基础，熟悉 API 和数据建模，并具备产品思维。
有 Python 或 LLM 产品经验者优先。"""

CHINESE_RESUME = """产品工程师
主导 React 与 TypeScript 产品重构，服务 1.8 万个团队，并通过用户反馈推动产品决策。
使用 PostgreSQL 完成数据建模，并构建过 Python 服务。"""


class AgentContractTests(unittest.TestCase):
    def setUp(self):
        self.job_agent = JobAnalyst()
        self.matcher_agent = ResumeMatcher()
        self.gap_agent = GapAnalyst()
        self.writer_agent = Writer()
        self.critic_agent = Critic()

    def test_agents_return_structured_outputs(self):
        job_output = self.job_agent.run(JOB)
        match_output = self.matcher_agent.run(job_output.requirements, RESUME)
        gap_output = self.gap_agent.run(job_output.requirements, match_output)
        writer_output = self.writer_agent.run(match_output, RESUME)
        critic_output = self.critic_agent.run(JOB, RESUME, job_output, match_output, writer_output)

        self.assertGreater(len(job_output.requirements), 0)
        self.assertEqual(len(match_output.matches), len(job_output.requirements))
        self.assertTrue(all(requirement.id.startswith("req-") for requirement in job_output.requirements))
        self.assertTrue(all(match.id.startswith("match-") for match in match_output.matches))
        self.assertTrue(all(gap.id.startswith("gap-") for gap in gap_output.gaps))
        self.assertIn(writer_output.status, {"review_required", "unsupported"})
        self.assertIn(critic_output.verdict, {"pass", "blocked"})

    def test_every_evidence_span_maps_exactly_to_source(self):
        job_output = self.job_agent.run(JOB)
        match_output = self.matcher_agent.run(job_output.requirements, RESUME)
        sources = {"job_description": JOB, "resume": RESUME}

        spans = [requirement.evidence for requirement in job_output.requirements]
        spans.extend(span for match in match_output.matches for span in match.evidence)
        self.assertGreater(len(spans), 0)
        for span in spans:
            source = sources[span.source]
            self.assertGreaterEqual(span.start, 0)
            self.assertLessEqual(span.end, len(source))
            self.assertLess(span.start, span.end)
            self.assertEqual(source[span.start:span.end], span.text)

    def test_writer_does_not_invent_when_resume_has_no_evidence(self):
        job = "The role requires strong Python experience."
        resume = "Managed a retail team and improved weekly scheduling."
        job_output = self.job_agent.run(job)
        match_output = self.matcher_agent.run(job_output.requirements, resume)
        writer_output = self.writer_agent.run(match_output, resume)

        self.assertEqual(writer_output.status, "unsupported")
        self.assertEqual(writer_output.edits, [])
        self.assertTrue(all(match.status == "unsupported" for match in match_output.matches))

    def test_critic_blocks_a_forged_writer_claim(self):
        job_output = self.job_agent.run(JOB)
        match_output = self.matcher_agent.run(job_output.requirements, RESUME)
        source = next(match.evidence[0] for match in match_output.matches if match.evidence)
        forged = WriterOutput(
            edits=[
                DraftEdit(
                    id="edit-forged",
                    target="resume_bullet",
                    before="",
                    after="Increased revenue by 400%.",
                    evidence_ids=[source.id],
                )
            ],
            status="review_required",
            message="test",
        )

        output = self.critic_agent.run(JOB, RESUME, job_output, match_output, forged)
        self.assertEqual(output.verdict, "blocked")
        self.assertIn("unsupported_claim", {issue.type for issue in output.issues})

    def test_chinese_inputs_keep_structured_evidence(self):
        job_output = self.job_agent.run(CHINESE_JOB)
        match_output = self.matcher_agent.run(job_output.requirements, CHINESE_RESUME)
        labels = {requirement.label for requirement in job_output.requirements}

        self.assertTrue({"React", "TypeScript", "产品思维", "API", "数据建模", "Python", "AI / LLM 产品"}.issubset(labels))
        self.assertTrue(any(match.status == "supported" for match in match_output.matches))
        react_match = next(match for match in match_output.matches if match.label == "React")
        self.assertIn("1.8 万", react_match.evidence[0].text)
        for requirement in job_output.requirements:
            self.assertEqual(CHINESE_JOB[requirement.evidence.start:requirement.evidence.end], requirement.evidence.text)


if __name__ == "__main__":
    unittest.main()
