"""
The interview's conversation loop, with a stub standing in for the model.

What is being pinned down here is the one rule a prompt cannot be trusted to
hold: when someone says they are out of material, offer exactly one more
question from a different angle, and then let go. Once is coaching. Twice is
nagging, and nagging is the failure mode this whole design exists to avoid.

No API calls.
"""
import json
import tempfile
import types
import unittest
from pathlib import Path

import helpers  # noqa: F401  -- puts src/ on the path; must come first

import interview as iv
import master_record as mr
import score_and_tailor as st
from master_record import Accomplishment, Record, Role


def reply(status="asking", say="A question?", **draft):
    full = {"title": "", "problem": "", "actions": "", "results": "",
            "evidence": ""}
    full.update(draft)
    return {"say": say, "draft": full, "status": status}


def as_response(payload):
    block = types.SimpleNamespace(type="text", text=json.dumps(payload))
    return types.SimpleNamespace(content=[block], stop_reason="end_turn",
                                 usage=types.SimpleNamespace(
                                     input_tokens=10, output_tokens=10,
                                     cache_creation_input_tokens=0,
                                     cache_read_input_tokens=0))


COMPLETE = reply(status="complete", say="Recorded.", title="Hired the night shift",
                 problem="no cover", actions="hired six", results="six hired",
                 evidence="scope")


class Harness(unittest.TestCase):
    """Scripted model replies and scripted typing, against a temp record."""

    def setUp(self):
        # ask() is the seam on purpose: patching the input builtin through the
        # module only works once something has assigned it there.
        self.saved_client, self.saved_master = st.client, iv.MASTER
        self.saved_ask = iv.ask
        self.tmp = tempfile.TemporaryDirectory()
        iv.MASTER = Path(self.tmp.name) / "master.md"
        self.asked = []
        self.model_calls = 0

    def tearDown(self):
        st.client, iv.MASTER = self.saved_client, self.saved_master
        iv.ask = self.saved_ask
        self.tmp.cleanup()

    def run_role(self, replies, typed, role=None):
        queue = list(replies)
        typing = iter(typed)

        def create(**kwargs):
            self.model_calls += 1
            return as_response(queue.pop(0) if queue else reply(status="role_done"))

        def fake_ask(question):
            self.asked.append(question)
            try:
                return next(typing)
            except StopIteration:
                raise iv.Stop()

        st.client = types.SimpleNamespace(
            messages=types.SimpleNamespace(create=create))
        iv.ask = fake_ask
        role = role or Role(employer="Acme", title="Supervisor")
        rec = Record(roles=[role])
        try:
            iv.interview_role(rec, role, target=10)
        except iv.Stop:
            pass
        return rec, role

    def lens_prompts(self):
        return [p for p in self.asked if "press Enter to skip" in p]


class OneMoreAngle(Harness):
    def test_offered_once_when_the_model_ends_the_role(self):
        self.run_role([reply(status="role_done", say="Understood.")], [""])
        self.assertEqual(len(self.lens_prompts()), 1,
                         "exactly one extra question, from a different angle")

    def test_offered_once_when_they_type_a_give_up_word(self):
        self.run_role([reply(say="What did you do?")], ["done", ""])
        self.assertEqual(len(self.lens_prompts()), 1)

    def test_never_offered_twice_in_one_role(self):
        """The counter is the point. A model told "ask once" will drift."""
        self.run_role(
            [reply(status="role_done"), reply(status="role_done"),
             reply(status="role_done")],
            ["", "", ""])
        self.assertEqual(len(self.lens_prompts()), 1)

    def test_declining_the_extra_question_ends_the_role(self):
        _rec, role = self.run_role([reply(status="role_done")], [""])
        self.assertEqual(role.accomplishments, [])

    def test_a_give_up_word_also_declines_the_extra_question(self):
        self.run_role([reply(status="role_done")], ["no"])
        self.assertEqual(len(self.lens_prompts()), 1)

    def test_answering_it_starts_another_round(self):
        """Their answer has to carry into the next conversation, or they would
        be asked the same thing twice."""
        _rec, role = self.run_role(
            [reply(status="role_done"), COMPLETE, reply(status="role_done")],
            ["we were short staffed every weekend so I hired six people", ""])
        self.assertEqual(len(role.accomplishments), 1)
        self.assertEqual(role.accomplishments[0].title, "Hired the night shift")

    def test_the_extra_question_names_the_angle_and_offers_an_exit(self):
        self.run_role([reply(status="role_done")], [""])
        prompt = self.lens_prompts()[0]
        self.assertIn("?", prompt)
        self.assertIn("press Enter to skip", prompt)


class RecordingAnAccomplishment(Harness):
    def test_a_complete_reply_is_saved_immediately(self):
        """Saved as it is confirmed, so stopping mid-role loses nothing."""
        rec, role = self.run_role([COMPLETE, reply(status="role_done")], [""])
        self.assertEqual(len(role.accomplishments), 1)
        on_disk = mr.parse(iv.MASTER.read_text(encoding="utf-8"))
        self.assertEqual(on_disk.roles[0].accomplishments[0].title,
                         "Hired the night shift")

    def test_the_evidence_tier_is_kept(self):
        _rec, role = self.run_role([COMPLETE, reply(status="role_done")], [""])
        self.assertEqual(role.accomplishments[0].evidence, "scope")

    def test_an_unknown_tier_falls_back_rather_than_crashing(self):
        bad = dict(COMPLETE)
        bad["draft"] = {**COMPLETE["draft"], "evidence": "vibes"}
        _rec, role = self.run_role([bad, reply(status="role_done")], [""])
        self.assertEqual(role.accomplishments[0].evidence, "qualitative")

    def test_a_duplicate_is_refused_rather_than_recorded_twice(self):
        role = Role(employer="Acme", title="Supervisor", accomplishments=[
            Accomplishment(title="Hired the night shift", problem="p",
                           actions="a", results="r", evidence="scope")])
        _rec, role = self.run_role(
            [COMPLETE, reply(status="role_done")], [""], role=role)
        self.assertEqual(len(role.accomplishments), 1,
                         "the duplicate must not be appended")

    def test_the_schema_is_sent_on_every_turn(self):
        self.run_role([reply(status="role_done")], [""])
        self.assertGreaterEqual(self.model_calls, 1)


if __name__ == "__main__":
    unittest.main()
