"""First names stay first names, unless two players share one.

The pick grid and the week chips only have room for a first name. That is
fine until two people are both Stephen, at which point both columns say
"Stephen" and nobody can tell whose pick is whose. A shared first name
picks up the first letter of the last name, and only then.

Run with: python tests/test_short_labels.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils import short_labels


class Person:
    def __init__(self, id, first_name, last_name):
        self.id = id
        self.first_name = first_name
        self.last_name = last_name


def test_a_unique_first_name_is_left_alone():
    labels = short_labels([
        Person(1, "Jeffrey", "DuBois"),
        Person(2, "Mira", "Moth"),
    ])
    assert labels == {1: "Jeffrey", 2: "Mira"}, labels


def test_a_shared_first_name_gains_the_last_initial():
    """The two Stephens, and nobody else."""
    labels = short_labels([
        Person(1, "Stephen", "Davis"),
        Person(2, "Stephen", "Miller"),
        Person(3, "Jeffrey", "DuBois"),
    ])
    assert labels[1] == "Stephen D", labels
    assert labels[2] == "Stephen M", labels
    assert labels[3] == "Jeffrey", labels


def test_the_match_ignores_capitalisation():
    labels = short_labels([
        Person(1, "Stephen", "Abbott"),
        Person(2, "stephen", "Baker"),
    ])
    assert labels[1] == "Stephen A", labels
    assert labels[2] == "stephen B", labels


def test_a_shared_initial_keeps_another_letter():
    """Two Stephens whose last names both start with D still have to differ."""
    labels = short_labels([
        Person(1, "Stephen", "Davis"),
        Person(2, "Stephen", "Dixon"),
    ])
    assert labels[1] == "Stephen Da", labels
    assert labels[2] == "Stephen Di", labels


def test_rows_that_carry_a_user_count_too():
    labels = short_labels([
        {"user": Person(1, "Ann", "Lee")},
        {"user": Person(2, "Ann", "Ng")},
    ])
    assert labels == {1: "Ann L", 2: "Ann N"}, labels


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"{len(tests)} passed")
