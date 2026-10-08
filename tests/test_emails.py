from gitscout.emails import (
    extract_emails_from_text,
    is_junk_email,
    name_matches,
    normalize_email,
    score,
)


def test_normalize_valid():
    assert normalize_email("  Foo@Acme.DEV ") == "foo@acme.dev"
    assert normalize_email("mailto:Jane@Acme.dev?subject=hi") == "jane@acme.dev"
    assert normalize_email("<bob@acme.dev>,") == "bob@acme.dev"


def test_normalize_rejects_junk():
    assert normalize_email(None) is None
    assert normalize_email("") is None
    assert normalize_email("not-an-email") is None
    assert normalize_email("12345+user@users.noreply.github.com") is None
    assert normalize_email("noreply@acme.dev") is None
    assert normalize_email("no-reply@acme.dev") is None
    assert normalize_email("logo@2x.png") is None
    assert normalize_email("someone@example.com") is None
    assert normalize_email("root@host.local") is None
    assert normalize_email("a@b") is None


def test_is_junk_email():
    assert is_junk_email("x@users.noreply.github.com")
    assert is_junk_email("donotreply@corp.io")
    assert not is_junk_email("dev@corp.io")


def test_extract_emails_from_html():
    html = (
        '<a href="mailto:hi@acme.dev">mail</a> contact: bob@acme.dev '
        "again hi@acme.dev <img src='logo@2x.png'> noreply@acme.dev"
    )
    assert extract_emails_from_text(html) == ["hi@acme.dev", "bob@acme.dev"]
    assert extract_emails_from_text("") == []


def test_name_matches():
    assert name_matches("jdoe", "John Doe", "John Doe")
    assert name_matches("jdoe", "John Doe", "jdoe")
    assert name_matches("x", "Ayush Kumar Singh", "Ayush Singh")
    assert not name_matches("jdoe", "John Doe", "Someone Else")
    assert not name_matches("jdoe", None, "Random Person")
    assert not name_matches("jdoe", "John Doe", None)
    # one shared first name is not enough when the profile name has several tokens
    assert not name_matches("jdoe", "John Doe", "John")


def test_score_order_and_penalty():
    assert score("profile") > score("commit_api") > score("events") > score("website")
    assert score("events", name_match=False) == score("events") / 2
    assert score("unknown-source") == 0.3
