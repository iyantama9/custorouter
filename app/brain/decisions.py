"""
Decisions Module - Track decisions and extract facts from conversations

Automatically identifies and tracks:
- Decisions made during conversations
- Facts learned about users/projects
- Important context and outcomes
"""

import re
from typing import List, Dict, Any, Optional
from app.brain.storage import BrainStorage


class DecisionTracker:
    """Track decisions and facts from conversations"""

    # Decision keywords/patterns, paired with the decision type they signal
    DECISION_PATTERNS = [
        (r"(?i)I (?:decided|chose|selected|picked) (?:to )?(.+)", "choice"),
        (r"(?i)(?:I'll|I will|Let's) (?:use|go with|implement|choose) (.+)", "commitment"),
        (r"(?i)(?:We|I) (?:should|must|need to) (.+)", "recommendation"),
        (r"(?i)Decision: (.+)", "explicit"),
        (r"(?i)(?:My|The) decision is (?:to )?(.+)", "explicit"),
    ]

    # Fact patterns (user preferences, information)
    FACT_PATTERNS = [
        (r"(?i)I (?:prefer|like|love|want|need) (.+)", "preference"),
        (r"(?i)My (.+) is (.+)", "profile"),
        (r"(?i)I (?:am|work as|do) (.+)", "profile"),
        (r"(?i)I'm using (.+)", "technology"),
        (r"(?i)The project (?:uses|is using|has) (.+)", "project"),
        (r"(?i)(?:Aku|Saya) (?:lebih suka|suka|ingin|butuh|mau) (.+)", "preference"),
        (r"(?i)(?:Aku|Saya) (?:pakai|menggunakan) (.+)", "technology"),
        (r"(?i)(?:Aku|Saya) bekerja sebagai (.+)", "profile"),
    ]

    # Outcome feedback patterns - signal whether the previous exchange landed well.
    # Matched against the user's *next* message, in English and Indonesian.
    NEGATIVE_OUTCOME_PATTERNS = [
        r"(?i)\bthat('?s| is)? (?:wrong|bad|broken|incorrect|not right)\b",
        r"(?i)\b(?:doesn'?t|didn'?t|isn'?t|not) work(?:ing)?\b",
        r"(?i)\bstill (?:broken|failing|wrong|not working)\b",
        r"(?i)\b(?:revert|undo|roll ?back) (?:that|this|it)\b",
        r"(?i)\b(?:salah|gagal|keliru|error lagi|masih error|nggak jalan|tidak jalan|nggak bisa)\b",
        r"(?i)\bbalikin (?:aja|ke|semula)\b",
    ]
    POSITIVE_OUTCOME_PATTERNS = [
        r"(?i)\bthat('?s| is)? (?:great|perfect|correct|working|fixed|good)\b",
        r"(?i)\bworks? now\b",
        r"(?i)\bfixed it\b",
        r"(?i)\bthanks?,? that (?:worked|fixed it)\b",
        r"(?i)\b(?:mantap|berhasil|jalan sekarang|udah (?:jalan|bener|betul)|makasih.*(?:jalan|berhasil))\b",
    ]

    @staticmethod
    async def extract_and_save_decisions(
        content: str,
        api_key_hash: str,
        session_id: int,
        role: str = "user"
    ):
        """
        Extract decisions from message content and save them.

        Args:
            content: Message content
            api_key_hash: User's API key hash
            session_id: Session ID
            role: Message role (decisions usually from user or assistant)
        """
        # The assistant's proposals are not the user's decisions.
        if role != "user":
            return 0
        decisions = DecisionTracker.extract_decisions(content)

        for decision in decisions:
            await BrainStorage.save_decision(
                api_key_hash=api_key_hash,
                session_id=session_id,
                title=decision["title"],
                description=decision.get("description"),
                context=decision.get("context"),
                decision_type=decision.get("type")
            )
        return len(decisions)

    @staticmethod
    def extract_decisions(content: str) -> List[Dict[str, Any]]:
        """
        Extract decisions from text using pattern matching.

        Args:
            content: Text to analyze

        Returns:
            List of extracted decisions
        """
        decisions = []
        content = DecisionTracker._memory_candidate_text(content)

        for pattern, decision_type in DecisionTracker.DECISION_PATTERNS:
            matches = re.findall(pattern, content)
            for match in matches:
                decision_text = DecisionTracker._clean_capture(match)
                if len(decision_text) > 10:  # Filter out too short matches
                    decisions.append({
                        "title": decision_text[:200],  # Truncate long titles
                        "description": None,
                        "context": content[:500],  # Save context
                        "type": decision_type
                    })

        return decisions

    @staticmethod
    async def extract_and_save_facts(
        content: str,
        api_key_hash: str,
        session_id: int,
        role: str = "user"
    ):
        """
        Extract facts from message content and save them.

        Args:
            content: Message content
            api_key_hash: User's API key hash
            session_id: Session ID
            role: Message role (facts usually from user messages)
        """
        if role != "user":
            return 0
        facts = DecisionTracker.extract_facts(content)

        saved = 0
        for fact in facts:
            was_saved = await BrainStorage.save_fact(
                api_key_hash=api_key_hash,
                session_id=session_id,
                fact=fact["fact"],
                category=fact.get("category"),
                source=f"conversation_{session_id}",
                confidence=fact.get("confidence", 0.8)
            )
            saved += bool(was_saved)
        return saved

    @staticmethod
    def extract_facts(content: str) -> List[Dict[str, Any]]:
        """
        Extract facts from text using pattern matching.

        Args:
            content: Text to analyze

        Returns:
            List of extracted facts
        """
        facts = []
        content = DecisionTracker._memory_candidate_text(content)

        for pattern, category in DecisionTracker.FACT_PATTERNS:
            for match in re.finditer(pattern, content):
                fact_text = DecisionTracker._clean_capture(match.group(0))

                if len(fact_text) > 5:  # Filter out too short matches
                    facts.append({
                        "fact": fact_text[:500],  # Truncate long facts
                        "category": category,
                        "confidence": 0.8  # Pattern-based extraction has medium confidence
                    })

        return facts

    @staticmethod
    def _memory_candidate_text(content: str) -> str:
        """Avoid treating quoted examples or code as durable user preferences."""
        without_code = re.sub(r"```[\s\S]*?```", "", content)
        return "\n".join(
            line for line in without_code.splitlines()
            if not line.lstrip().startswith((">", "    "))
        )[:8000]

    @staticmethod
    def _clean_capture(value: str) -> str:
        return re.split(r"[.!?](?:\s|$)", value.strip(), maxsplit=1)[0].strip()[:500]

    @staticmethod
    def _categorize_fact(fact_text: str) -> str:
        """Categorize a fact based on its content"""
        text_lower = fact_text.lower()

        if any(word in text_lower for word in ["prefer", "like", "love", "want", "need"]):
            return "preference"
        elif any(word in text_lower for word in ["work", "job", "developer", "engineer"]):
            return "profile"
        elif any(word in text_lower for word in ["project", "app", "application", "system"]):
            return "project"
        elif any(word in text_lower for word in ["using", "use", "technology", "framework", "library"]):
            return "technology"
        else:
            return "general"

    @staticmethod
    def detect_outcome_feedback(content: str) -> Optional[str]:
        """
        Check whether a message expresses positive or negative feedback about
        whatever came right before it (typically the previous assistant reply).

        Returns "negative", "positive", or None.
        """
        for pattern in DecisionTracker.NEGATIVE_OUTCOME_PATTERNS:
            if re.search(pattern, content):
                return "negative"
        for pattern in DecisionTracker.POSITIVE_OUTCOME_PATTERNS:
            if re.search(pattern, content):
                return "positive"
        return None

    @staticmethod
    async def apply_outcome_feedback(
        content: str,
        api_key_hash: str,
        session_id: int
    ):
        """
        If this message reads as feedback on the previous exchange, close the
        loop: resolve the latest open decision in this session, and — since we
        know which model actually answered — log a model_feedback decision so
        routing can learn which models this user has had bad luck with.
        """
        sentiment = DecisionTracker.detect_outcome_feedback(content)
        if not sentiment:
            return False

        # Resolve the most recent still-open decision for this session, if any.
        open_decision = await BrainStorage.get_latest_unresolved_decision(
            api_key_hash=api_key_hash,
            session_id=session_id
        )
        if open_decision:
            await BrainStorage.update_decision_outcome(open_decision["id"], sentiment)

        # Tie the feedback to the model that produced the previous reply.
        last_model = await BrainStorage.get_last_assistant_model(session_id)
        if not last_model:
            return bool(open_decision)

        model_ref = last_model.split("/", 1)[1] if "/" in last_model else last_model
        await BrainStorage.save_decision(
            api_key_hash=api_key_hash,
            session_id=session_id,
            decision_type="model_feedback",
            title=f"Model {last_model} received {sentiment} feedback",
            description=None,
            context=content[:300],
            outcome=sentiment,
            model_ref=model_ref
        )
        return True

    @staticmethod
    async def analyze_conversation(
        content: str,
        api_key_hash: str,
        session_id: int,
        role: str
    ):
        """
        Analyze a conversation message and extract decisions and facts.

        Args:
            content: Message content
            api_key_hash: User's API key hash
            session_id: Session ID
            role: Message role
        """
        # Extract and save decisions
        new_decisions = await DecisionTracker.extract_and_save_decisions(
            content=content,
            api_key_hash=api_key_hash,
            session_id=session_id,
            role=role
        )

        # Extract and save facts, resolve outcomes, and refresh the cached
        # profile — all from the user's side of the conversation.
        if role == "user":
            new_facts = await DecisionTracker.extract_and_save_facts(
                content=content,
                api_key_hash=api_key_hash,
                session_id=session_id,
                role=role
            )
            feedback_changed = await DecisionTracker.apply_outcome_feedback(
                content=content,
                api_key_hash=api_key_hash,
                session_id=session_id
            )
            if new_facts or new_decisions or feedback_changed:
                await DecisionTracker.get_user_profile(api_key_hash, persist=True)

    @staticmethod
    async def get_user_profile(api_key_hash: str, persist: bool = False) -> Dict[str, Any]:
        """
        Build a user profile from extracted facts.

        Args:
            api_key_hash: User's API key hash
            persist: Also upsert the result into brain_profiles as a materialized
                cache (called after every user turn so the table stays live).

        Returns:
            User profile dictionary
        """
        # Get all facts
        all_facts = await BrainStorage.get_facts(
            api_key_hash=api_key_hash,
            limit=500
        )

        # Group by category
        profile = {
            "preferences": [],
            "profile": [],
            "projects": [],
            "technologies": [],
            "general": []
        }

        for fact in all_facts:
            category = fact.get("category", "general")
            if category in profile:
                profile[category].append(fact)

        # Get recent decisions
        decisions = await BrainStorage.get_decisions(
            api_key_hash=api_key_hash,
            limit=50
        )

        profile["recent_decisions"] = decisions[:10]

        # Model preference signal, surfaced from closed-loop feedback.
        profile["avoided_models"] = sorted(await BrainStorage.get_avoided_models(api_key_hash))

        if persist:
            await BrainStorage.save_profile(api_key_hash, profile)

        return profile
