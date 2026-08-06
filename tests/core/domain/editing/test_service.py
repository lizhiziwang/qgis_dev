"""测试 :class:`app.core.domain.editing.service.LayerEditService`。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("qgis", reason="LayerEditService 用 qgis.core.QgsMapLayer")


from app.core.domain.editing.service import LayerEditService


def test_start_with_non_vector_returns_false():
    svc = LayerEditService()
    layer = MagicMock()
    layer.type = MagicMock(return_value=0)  # 不是 VectorLayer
    layer.VectorLayer = 2
    assert svc.start(layer) is False


def test_start_already_editable_returns_true():
    svc = LayerEditService()
    layer = MagicMock()
    layer.type = MagicMock(return_value=2)  # VectorLayer
    layer.VectorLayer = 2
    layer.isEditable.return_value = True
    assert svc.start(layer) is True
    layer.startEditing.assert_not_called()


def test_start_calls_start_editing():
    svc = LayerEditService()
    layer = MagicMock()
    layer.type = MagicMock(return_value=2)
    layer.VectorLayer = 2
    layer.isEditable.return_value = False
    layer.startEditing.return_value = True
    assert svc.start(layer) is True
    layer.startEditing.assert_called_once()


def test_start_returns_false_when_qgis_returns_false():
    svc = LayerEditService()
    layer = MagicMock()
    layer.type = MagicMock(return_value=2)
    layer.VectorLayer = 2
    layer.isEditable.return_value = False
    layer.startEditing.return_value = False
    assert svc.start(layer) is False


def test_stop_saves_and_commits():
    svc = LayerEditService()
    layer = MagicMock()
    layer.isEditable.return_value = True
    layer.commitChanges.return_value = True
    assert svc.stop(layer, save=True) is True
    layer.commitChanges.assert_called_with(stopEditing=True)


def test_stop_rolls_back_when_save_false():
    svc = LayerEditService()
    layer = MagicMock()
    layer.isEditable.return_value = True
    layer.rollBack.return_value = True
    layer.commitChanges.return_value = True
    assert svc.stop(layer, save=False) is True
    layer.rollBack.assert_called_once()


def test_stop_when_not_editable():
    svc = LayerEditService()
    layer = MagicMock()
    layer.isEditable.return_value = False
    assert svc.stop(layer) is True
    layer.commitChanges.assert_not_called()


def test_is_modified():
    svc = LayerEditService()
    layer = MagicMock()
    layer.isEditable.return_value = True
    layer.isModified.return_value = True
    assert svc.is_modified(layer) is True
    layer.isEditable.return_value = False
    assert svc.is_modified(layer) is False


def test_commit_with_stop():
    svc = LayerEditService()
    layer = MagicMock()
    layer.isEditable.return_value = True
    layer.commitChanges.return_value = True
    assert svc.commit(layer, stop_editing=False) is True
    layer.commitChanges.assert_called_with(stopEditing=False)
