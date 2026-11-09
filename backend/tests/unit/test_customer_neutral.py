"""The next tender's customer is not the last tender's customer.

The obligation-owner work was built against an ADX tender, and the customer's
name leaked into two places that outlive it:

  * the classifier's examples, every one of which said "ADX may waive...",
    teaching the model a NAME rather than the shape of a reserved right;
  * the matrix's own label, which said "ADX term" on every customer-owned row.

The second was visible the first time a Jadwa RFP was loaded: three clauses
Jadwa reserves for itself, each labelled an ADX term. Cosmetic on its face, and
not cosmetic at all in a product whose entire claim is that one bid's material
never contaminates another's.

These are cheap tests for a cheap mistake that is easy to make again: the next
person tuning a prompt against a live tender will reach for that tender's
clauses, because they are what is in front of them.
"""

from __future__ import annotations

from app.gateway import registry

# Names of real customers this system has been run against. A prompt may
# mention them as ONE OF SEVERAL examples; it may not be built on one.
CUSTOMERS = ("ADX", "Jadwa")


def test_the_classifier_teaches_a_shape_not_a_name():
    prompt = registry.get("requirement_decomposition").system_prompt
    assert "match the SHAPE of the clause, never a particular name" in prompt


def test_no_single_customer_dominates_the_examples():
    """One mention as an illustration is fine. Three worked examples all naming
    the same customer is a prompt fitted to one tender."""
    prompt = registry.get("requirement_decomposition").system_prompt
    for name in CUSTOMERS:
        assert prompt.count(name) <= 1, f"{name} appears too often to be an example"


def test_no_prompt_in_the_registry_is_built_around_one_customer():
    """The whole registry, not just the one that went wrong."""
    for call_type in registry.all_call_types().values():
        for name in CUSTOMERS:
            assert call_type.system_prompt.count(name) <= 1, (
                f"{call_type.name} mentions {name} more than once"
            )


def test_the_prompt_version_moved_when_the_wording_did():
    """Prompts are versioned so a cached answer from the ADX-shaped wording is
    not served against the neutral one."""
    # Compared as a number, not as a string: "v10" < "v4" lexicographically,
    # so the string form quietly starts failing at the tenth version.
    version = registry.get("requirement_decomposition").prompt_version
    assert int(version.lstrip("v").split(".")[0]) >= 4
