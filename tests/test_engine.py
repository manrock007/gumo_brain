from app.engine import BUILD_GROUP_RE, extract_questions_last, parse_stage_output


class TestParseStageOutput:
    def test_done_with_payload(self):
        marker, payload, pr = parse_stage_output(
            "I explored the repo.\n\nSTAGE_DONE:\n## Understanding\nstuff\n\n## Questions\n1. ok?"
        )
        assert marker == "done"
        assert payload.startswith("## Understanding")
        assert pr is None

    def test_fail(self):
        marker, payload, _ = parse_stage_output("STAGE_FAIL: request too vague, need the target screen")
        assert marker == "fail"
        assert "too vague" in payload

    def test_unparsed_fails_closed(self):
        marker, payload, _ = parse_stage_output("I did lots of stuff and forgot the marker")
        assert marker == "unparsed"

    def test_last_marker_wins(self):
        text = "STAGE_FAIL: first thoughts\n...more work...\nSTAGE_DONE:\nfinal answer\n## Questions\n1. ok?"
        marker, payload, _ = parse_stage_output(text)
        assert marker == "done"
        assert payload.startswith("final answer")

    def test_marker_must_be_line_start(self):
        marker, _, _ = parse_stage_output("as discussed STAGE_DONE: is the protocol")
        assert marker == "unparsed"

    def test_bare_pr_url_never_counts(self):
        marker, _, pr = parse_stage_output(
            "see https://github.com/x/y/pull/9 for context\nSTAGE_DONE:\nsummary\n## Questions\n1. ok?"
        )
        assert marker == "done"
        assert pr is None

    def test_standalone_pr_line_is_recorded(self):
        text = ("work done\nPR_URL: https://github.com/acme/web/pull/42\n"
                "STAGE_DONE:\nbuilt group 1\n## Questions\n1. approve?")
        marker, _, pr = parse_stage_output(text)
        assert marker == "done"
        assert pr == "https://github.com/acme/web/pull/42"

    def test_pr_line_tolerates_backticks_and_bullets(self):
        # models often wrap the line; the regex must still find it
        for line in (
            "PR_URL: `https://github.com/x/y/pull/7`",
            "- PR_URL: https://github.com/x/y/pull/7",
            "> PR_URL:  https://github.com/x/y/pull/7",
        ):
            _, _, pr = parse_stage_output(f"{line}\nSTAGE_DONE:\nok\n## Questions\n1. ok?")
            assert pr == "https://github.com/x/y/pull/7", line

    def test_empty(self):
        assert parse_stage_output("")[0] == "unparsed"


class TestBuildGroupRegex:
    def test_matches_h2_and_h3_and_case(self):
        assert len(BUILD_GROUP_RE.findall("## Build group 1\n...\n## Build group 2\n")) == 2
        assert len(BUILD_GROUP_RE.findall("### build group A\n#### Build Group B\n")) == 2

    def test_single_group(self):
        assert len(BUILD_GROUP_RE.findall("## Build group 1\nonly one")) == 1


class TestExtractQuestionsLast:
    def test_last_heading_wins(self):
        text = "## Questions\n1. embedded from P1?\n\n## Design\n...\n\n## Questions\n1. real gate question?"
        assert extract_questions_last(text) == "1. real gate question?"

    def test_fallback_tail(self):
        assert extract_questions_last("just prose") == "just prose"
