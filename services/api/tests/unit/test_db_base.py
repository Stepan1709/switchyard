from sqlalchemy import Column, ForeignKey, Integer, String, Table
from sqlalchemy.schema import CreateTable

from app.core.db import Base, TimestampMixin, UUIDPrimaryKeyMixin


def test_naming_convention_gives_deterministic_constraint_names():
    parent = Table("parent", Base.metadata, Column("id", Integer, primary_key=True))
    child = Table(
        "child",
        Base.metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", ForeignKey("parent.id")),
        Column("name", String, unique=True),
    )
    ddl = str(CreateTable(child))
    assert "pk_child" in ddl
    assert "fk_child_parent_id_parent" in ddl
    assert "uq_child_name" in ddl
    Base.metadata.remove(child)
    Base.metadata.remove(parent)


def test_mixins_define_expected_columns():
    class Thing(UUIDPrimaryKeyMixin, TimestampMixin, Base):
        __tablename__ = "thing_for_test"

    cols = set(Thing.__table__.columns.keys())
    assert {"id", "created_at", "updated_at"} <= cols
    Base.metadata.remove(Thing.__table__)  # type: ignore[arg-type]
