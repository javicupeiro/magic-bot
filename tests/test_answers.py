from dataclasses import replace
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.generation.answers import AnswerDraft, AnswerService, Claim, NormalizedQuestion, validate_citations
from backend.generation.llm import LLM, LLMConfig, StructuredOutputError
from backend.generation.prompt_loader import load_prompt
from backend.retrieval.retrieval import SearchResult


SOURCE = SearchResult("mtg:edition:rule:106.4", "rule", "106.4", "Unused mana empties at the end of each step and phase.", 5, 6, "edition", 0.1)
GLOSSARY = SearchResult("mtg:edition:glossary:Mana", "glossary", "Mana", "The primary resource in the game.", 200, 200, "edition", 0.2)


def supported_draft():
    return AnswerDraft(can_answer=True, claims=[Claim(text="El maná no usado se vacía al final de cada paso y fase.", source_ids=[SOURCE.id])], assumptions=[])


class PromptTests(unittest.TestCase):
    def test_bundled_prompts_have_version_hash_and_strict_variables(self):
        for name, variables in [("normalize", {"question_json": '"$literal"'}),
                                ("answer", {"question_json": '"$literal"', "language": "es", "context_json": "[]"})]:
            prompt = load_prompt(name)
            self.assertEqual(prompt.name, name)
            self.assertEqual(prompt.version, "v1")
            path = Path("backend/generation/prompts") / name / "v1.md"
            self.assertEqual(prompt.sha256, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertIn('"$literal"', prompt.render(**variables))
            with self.assertRaisesRegex(ValueError, "missing variables"):
                prompt.render()
            with self.assertRaisesRegex(ValueError, "unexpected variables"):
                prompt.render(**variables, unexpected="value")

    def test_missing_file_and_invalid_header_version_variables(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaisesRegex(FileNotFoundError, "Prompt file does not exist"):
                load_prompt("normalize", root=root)
            path = root / "normalize" / "v1.md"
            path.parent.mkdir()
            for text in ["Name: normalize\nVersion: v2\nVariables: question_json\n\n$question_json",
                         "Name: wrong\nVersion: v1\nVariables: question_json\n\n$question_json",
                         "Name: normalize\nVersion: v1\nVariables: question_json\n\n$unknown",
                         "Name: normalize\nVersion: v1\nVariables: question_json\n\n${",
                         "Name: normalize\nVersion: v1\nVariables: question_json\n\n"]:
                with self.subTest(text=text):
                    path.write_text(text)
                    with self.assertRaisesRegex(ValueError, "Invalid prompt"):
                        load_prompt("normalize", root=root)
        with self.assertRaises(ValueError):
            load_prompt("../normalize")


class LLMTests(unittest.TestCase):
    def test_responses_structured_contract_without_sampling_parameters(self):
        client = Mock()
        output = NormalizedQuestion(language="en", english_query="mana")
        client.responses.parse.return_value = SimpleNamespace(status="completed", output_parsed=output)
        self.assertEqual(LLM(client=client).generate("prompt", NormalizedQuestion), output)
        client.responses.parse.assert_called_once_with(
            model="gpt-6-luna", reasoning={"effort": "high"},
            input=[{"role": "user", "content": "prompt"}], text_format=NormalizedQuestion, store=False)
        for status, parsed in [("incomplete", output), ("completed", None)]:
            client.responses.parse.return_value = SimpleNamespace(status=status, output_parsed=parsed)
            with self.assertRaises(StructuredOutputError):
                LLM(client=client).generate("prompt", NormalizedQuestion)

    def test_environment_and_dotenv_overrides(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text("OPENAI_API_KEY=fixture-key\nOPENAI_LLM_MODEL=from-file\nOPENAI_REASONING_EFFORT=low\n")
            from dotenv import load_dotenv
            with patch.dict(os.environ, {"OPENAI_LLM_MODEL": "from-env"}, clear=True), \
                    patch("backend.generation.llm.load_dotenv", side_effect=lambda: load_dotenv(env)), \
                    patch("backend.generation.llm.OpenAI") as provider:
                llm = LLM()
                self.assertEqual(llm.config, LLMConfig("from-env", "low"))
                provider.return_value.responses.parse.return_value = SimpleNamespace(
                    status="completed", output_parsed=NormalizedQuestion(language="en", english_query="mana"))
                llm.generate("prompt", NormalizedQuestion)
                provider.assert_called_once_with(api_key="fixture-key")
        with patch.dict(os.environ, {}, clear=True), patch("backend.generation.llm.load_dotenv"):
            with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
                LLM().generate("prompt", NormalizedQuestion)
        for config in [{"model": " "}, {"reasoning_effort": "invalid"}]:
            with self.assertRaises(ValueError):
                LLMConfig(**config)


class CitationTests(unittest.TestCase):
    def test_citations_use_stored_text_and_metadata_and_deduplicate(self):
        draft = supported_draft()
        draft.claims[0].source_ids.extend([SOURCE.id, GLOSSARY.id])
        citations = validate_citations(draft, [SOURCE, GLOSSARY])
        self.assertEqual([citation.source_id for citation in citations], [SOURCE.id, GLOSSARY.id])
        self.assertEqual(citations[0].model_dump(), {
            "source_id": SOURCE.id, "kind": "rule", "title": "106.4", "excerpt": SOURCE.text,
            "page_start": 5, "page_end": 6, "version": "edition"})
        self.assertEqual(validate_citations(AnswerDraft(can_answer=False, claims=[], assumptions=[]), []), [])

    def test_invalid_references_missing_evidence_and_malformed_answers(self):
        variants = [
            (supported_draft(), []),
            (supported_draft(), [replace(SOURCE, id="different-edition")]),
            (supported_draft(), [replace(SOURCE, text=" ")]),
            (AnswerDraft(can_answer=True, claims=[], assumptions=[]), [SOURCE]),
            (AnswerDraft(can_answer=False, claims=supported_draft().claims, assumptions=[]), [SOURCE]),
            (AnswerDraft(can_answer=False, claims=[], assumptions=["Missing premise"]), [SOURCE]),
            (AnswerDraft(can_answer=True, claims=[Claim(text=" ", source_ids=[SOURCE.id])], assumptions=[]), [SOURCE]),
            (AnswerDraft(can_answer=True, claims=[Claim(text="Mana [invented]", source_ids=[SOURCE.id])], assumptions=[]), [SOURCE]),
        ]
        for draft, context in variants:
            with self.subTest(draft=draft, context=context), self.assertRaises(ValueError):
                validate_citations(draft, context)


class AnswerServiceTests(unittest.TestCase):
    def service(self, language="es", draft=None, *, sources=True):
        llm = Mock(config=LLMConfig())
        llm.generate.side_effect = [NormalizedQuestion(language=language, english_query="When does unused mana empty?"),
                                    supported_draft() if draft is None else draft]
        retriever = Mock()
        retriever.search.side_effect = [[SOURCE], [GLOSSARY]] if sources else [[], []]
        return AnswerService(retriever, llm=llm)

    def test_original_language_query_sources_and_prompt_provenance(self):
        for language, question in [("es", " ¿Cuándo se vacía el maná no usado? "),
                                   ("en", "When does unused mana empty?")]:
            service = self.service(language)
            result = service.answer(question, k=3, version="edition")
            self.assertEqual(result.original_question, question)
            self.assertEqual(result.language, language)
            self.assertEqual(result.english_query, "When does unused mana empty?")
            self.assertTrue(result.can_answer)
            self.assertEqual(result.source_ids, [SOURCE.id])
            self.assertEqual(len(result.retrieved_sources), 2)
            self.assertEqual(result.prompts["normalize"]["version"], "v1")
            self.assertIn(f"[{SOURCE.id}]", result.answer)
            for kind in ("rule", "glossary"):
                service.retriever.search.assert_any_call(result.english_query, k=3, kind=kind, version="edition")
            answer_prompt = service.llm.generate.call_args_list[1].args[0]
            self.assertIn(f"({language}:", answer_prompt)
            self.assertIn(SOURCE.text, answer_prompt)
            self.assertIn(GLOSSARY.text, answer_prompt)

    def test_empty_context_skips_generation(self):
        service = self.service(sources=False)
        result = service.answer("¿Quién ganó el mundial?")
        self.assertFalse(result.can_answer)
        self.assertEqual(result.abstention_reason, "empty_context")
        self.assertEqual(service.llm.generate.call_count, 1)

    def test_abstentions_discard_all_unsupported_content(self):
        for language in ("en", "es"):
            for draft, expected in [
                (AnswerDraft(can_answer=False, claims=[], assumptions=[]), "insufficient_evidence"),
                (AnswerDraft(can_answer=True, claims=[Claim(text="Invented factual answer", source_ids=["fake"])], assumptions=["invented"]), "invalid_citations_or_answer"),
                (StructuredOutputError("refused"), "unavailable_structured_answer"),
            ]:
                with self.subTest(language=language, expected=expected):
                    result = self.service(language, draft).answer("question")
                    self.assertFalse(result.can_answer)
                    self.assertEqual(result.abstention_reason, expected)
                    self.assertEqual((result.claims, result.citations, result.source_ids, result.assumptions), ([], [], [], []))
                    self.assertNotIn("Invented", result.answer)
                    self.assertIn("No puedo" if language == "es" else "I cannot", result.answer)

    def test_invalid_inputs_do_not_call_provider(self):
        service = self.service()
        for question, k in [(" ", 5), ("mana", 0), ("mana", True)]:
            with self.assertRaises(ValueError):
                service.answer(question, k=k)
        service.llm.generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
