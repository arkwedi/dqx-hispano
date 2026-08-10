import csv
import sqlite3

# --- CONFIGURACIÓN ---
DB_PATH = "clarity_dialog.db"  # Ruta a tu archivo .db
CSV_PATH = "glosario.csv"  # Ruta a tu archivo .csv
NOMBRE_TABLA = "m00_strings"  # Nombre de la tabla en tu base de datos

# Nombre exacto de las columnas en la DB
COLUMNA_DB_KEY = "ja"  # Columna que sirve de cruce en la DB
COLUMNA_DB_TARGET = "en"  # Columna que quieres sobrescribir en la DB

# Nombre exacto de las columnas en la cabecera del CSV
COLUMNA_CSV_KEY = "ja"  # Columna de cruce en el CSV
COLUMNA_CSV_VALUE = "es"  # Columna con el nuevo texto en español


def actualizar_base_de_datos():
    # 1. Conectar a la base de datos SQLite
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 2. Leer el archivo CSV (ahora solo con columnas 'ja' y 'es')
    datos_para_actualizar = []
    with open(CSV_PATH, mode="r", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file, delimiter=";")
        for row in reader:
            # Toma directamente el valor de 'es' para reemplazar y 'ja' como clave
            val_es = row["es"].strip()
            val_ja = row["ja"].strip()

            if val_ja:
                datos_para_actualizar.append((val_es, val_ja))

    # 3. Ejecutar la actualización en lote (Batch Update)
    sql = f"""
        UPDATE {NOMBRE_TABLA}
        SET {COLUMNA_DB_TARGET} = ?
        WHERE {COLUMNA_DB_KEY} = ?
    """

    cursor.executemany(sql, datos_para_actualizar)

    # 4. Guardar cambios y cerrar conexión
    conn.commit()
    filas_afectadas = cursor.rowcount
    conn.close()

    print(f" ¡Listo! Se procesaron {len(datos_para_actualizar)} registros del CSV.")


if __name__ == "__main__":
    actualizar_base_de_datos()