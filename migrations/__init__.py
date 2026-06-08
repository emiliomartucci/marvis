# Marker so the migrations/ directory ships in the wheel (its *.sql files are
# package-data). The runtime resolves MIGRATIONS_DIR relative to core/api/db.py;
# in an installed wheel that points at <site-packages>/migrations, so the SQL
# must be packaged here for `marvis init` to bootstrap the schema.
