from storage import encoder


class _VectorBatch:
    def tolist(self):
        return [[0.1, 0.2], [0.3, 0.4]]


class _Model:
    def __init__(self):
        self.calls = []

    def encode(self, value):
        self.calls.append(value)
        return _VectorBatch()


def test_encode_batch_uses_one_model_call(monkeypatch):
    model = _Model()
    monkeypatch.setattr(encoder, "_get_model", lambda: model)

    assert encoder.encode_batch(["first", "second"]) == [[0.1, 0.2], [0.3, 0.4]]
    assert model.calls == [["first", "second"]]


def test_encode_batch_skips_model_for_empty_input(monkeypatch):
    monkeypatch.setattr(encoder, "_get_model", lambda: (_ for _ in ()).throw(AssertionError()))

    assert encoder.encode_batch([]) == []
