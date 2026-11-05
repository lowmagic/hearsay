import pytest

from hearsay.speakers import NOT_OWNER_THRESHOLD, OWNER_THRESHOLD, label_for


@pytest.mark.parametrize("similarity, label", [
    (OWNER_THRESHOLD, "owner"),
    (OWNER_THRESHOLD - 0.01, None),
    (NOT_OWNER_THRESHOLD, "not_owner"),
    (NOT_OWNER_THRESHOLD + 0.01, None),
])
def test_a_voice_is_labeled_from_its_threshold_on_and_left_alone_in_between(similarity, label):
    assert label_for(similarity) == label
