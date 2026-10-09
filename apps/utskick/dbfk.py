"""
Främmande nycklar med raderingsregeln i databasen (README B.0, uppföljningen
inför S2).

Django skapar en främmande nyckel utan ON DELETE (DEFERRABLE INITIALLY
DEFERRED) och sköter CASCADE och SET_NULL själv i Python. Det håller bara
så länge koden som raderar känner till tabellen som pekar. Den förra
versionen kör vidare mellan migrate och reload, och för gott efter en
automatisk tillbakarullning; den känner inte till S2:s tabeller. När den
tar bort en kontakt, en förfrågan, ett konto eller en användare som en
S2-rad pekar på blir det IntegrityError vid commit.

Django 6.0 har ingen on_delete på databasnivå (DB_CASCADE kom senare), så
migreringarna lägger regeln själva: varje ny främmande nyckel med CASCADE
eller SET_NULL får samma regel i Postgres. Pythons regel gäller som förut
(den körs först och lämnar inget kvar åt databasen); databasens regel tar
över när en äldre version raderar.

    apply(apps, schema_editor, fields)   i en RunPython sist i migreringen
    sql_action(field)                    "CASCADE", "SET NULL" eller None

fields är [(app_label, model_name, field_name)]. PROTECT och DO_NOTHING
lämnas orörda. Vakten test_s2_foundation.DbOnDeleteTests kräver regeln för
varje främmande nyckel från S2 och framåt som rör apps/utskick.
"""

from django.db import models

#: Djangos regel -> Postgres regel. confdeltype i pg_constraint: c, n.
ACTIONS = {models.CASCADE: "CASCADE", models.SET_NULL: "SET NULL"}
CONFDELTYPE = {"CASCADE": "c", "SET NULL": "n"}


def sql_action(field):
    """Postgres ON DELETE för fältets on_delete, eller None (lämnas)."""
    return ACTIONS.get(getattr(field.remote_field, "on_delete", None))


def _flush_deferred(schema_editor):
    """Django skjuter de främmande nycklarna för nya tabeller till slutet av
    migreringen (schema_editor.deferred_sql). Kör dem nu, så att de finns
    att ändra, och töm listan så att de inte körs två gånger."""
    for statement in list(schema_editor.deferred_sql):
        schema_editor.execute(statement)
    schema_editor.deferred_sql.clear()


def apply(apps, schema_editor, fields):
    """Ge varje fält i fields sin raderingsregel i databasen. Bara Postgres;
    körs i samma transaktion som migreringen."""
    connection = schema_editor.connection
    if connection.vendor != "postgresql":
        return
    _flush_deferred(schema_editor)
    quote = schema_editor.quote_name
    for app_label, model_name, field_name in fields:
        model = apps.get_model(app_label, model_name)
        field = model._meta.get_field(field_name)
        action = sql_action(field)
        if action is None or not field.db_constraint:
            continue
        table = model._meta.db_table
        target = field.related_model._meta.db_table
        target_column = field.target_field.column
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, table)
        names = [
            name
            for name, info in constraints.items()
            if info.get("foreign_key") and info.get("columns") == [field.column]
        ]
        if len(names) != 1:
            raise RuntimeError(
                f"Hittade {len(names)} främmande nycklar för {table}.{field.column}, väntade en."
            )
        name = names[0]
        schema_editor.execute(
            f"ALTER TABLE {quote(table)} DROP CONSTRAINT {quote(name)}, "
            f"ADD CONSTRAINT {quote(name)} FOREIGN KEY ({quote(field.column)}) "
            f"REFERENCES {quote(target)} ({quote(target_column)}) ON DELETE {action} "
            "DEFERRABLE INITIALLY DEFERRED"
        )


def rules(connection, table):
    """{kolumn: confdeltype} för tabellens främmande nycklar (vakten)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT a.attname, c.confdeltype
            FROM pg_constraint c
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
            WHERE c.contype = 'f' AND c.conrelid = %s::regclass
            """,
            [table],
        )
        return {column: rule for column, rule in cursor.fetchall()}
