"""Configuración leída del entorno (.env)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    pg_dsn: str = "postgresql://postgres:postgres@127.0.0.1:5432/biotime"

    # Ingesta ADMS. El puerto DEBE ser 83: los 4 terminales lo tienen grabado.
    host: str = "0.0.0.0"
    port: int = 83

    # API de consulta, en puerto y bind propios. Separarlo mantiene el endpoint
    # de credenciales fuera del puerto expuesto a internet, y permite migrar
    # consumo e ingesta en fases independientes.
    # El default es fail-closed: si nadie configura nada, el API solo responde
    # desde el propio host en lugar de quedar expuesto por descuido.
    api_host: str = "127.0.0.1"
    api_port: int = 8083

    # Defensa en profundidad. Horario360 corre en otro servidor, así que el API
    # escucha en 0.0.0.0 y el firewall es la barrera principal. Esta lista es la
    # segunda: si la regla del firewall se pierde en un reinicio o una
    # reinstalación, la aplicación sigue rechazando a quien no está autorizado.
    api_allowed_ips: str = ""

    # Offset del terminal cuando aún no está en iclock_terminal.
    device_tz_fallback: int = -5
    # Zona en la que el API serializa punch_time, replicando a BioTime.
    api_render_tz: str = "America/Lima"

    allowed_sns: str = ""
    allowed_source_ips: str = ""
    company_code: str = "1"
    store_raw_bodies: bool = True

    handshake_trans_flag: str = (
        "TransData AttLog OpLog AttPhoto EnrollUser ChgUser EnrollFP ChgFP FPImag"
    )
    handshake_trans_interval: int = 1
    handshake_timezone: int = -5
    handshake_realtime: int = 1

    jwt_secret: str = "cambiar-por-un-valor-aleatorio-largo"
    jwt_ttl_hours: int = 24
    api_default_page_size: int = 100
    api_max_page_size: int = 5000

    # Corrección de códigos mal enrolados (rutas /api/emp-code-corrections/).
    # Es la única parte de la aplicación que modifica marcaciones, así que va
    # apagada por defecto y usa su propia conexión con un rol que sí tiene
    # UPDATE (sql/005_fix_user.sql). El rol de PG_DSN sigue sin poder tocar
    # iclock_transaction: un error en la ingesta o en la consulta no puede
    # alterar marcaciones aunque quisiera.
    corrections_enabled: bool = False
    pg_fix_dsn: str = ""

    log_level: str = "INFO"

    @property
    def allowed_sn_set(self) -> set[str]:
        """SN autorizados. Vacío = sin restricción."""
        return {s.strip() for s in self.allowed_sns.split(",") if s.strip()}

    @property
    def allowed_ip_set(self) -> set[str]:
        """IPs de origen autorizadas para la ingesta. Vacío = sin restricción."""
        return {s.strip() for s in self.allowed_source_ips.split(",") if s.strip()}

    @property
    def api_allowed_ip_set(self) -> set[str]:
        """IPs autorizadas a consumir el API. Vacío = sin restricción."""
        return {s.strip() for s in self.api_allowed_ips.split(",") if s.strip()}

    @property
    def handshake_options(self) -> dict[str, str]:
        """Pares clave=valor que se devuelven al terminal en el handshake."""
        return {
            "ErrorDelay": "30",
            "Delay": "10",
            "TransTimes": "00:00;14:05",
            "TransInterval": str(self.handshake_trans_interval),
            "TransFlag": self.handshake_trans_flag,
            "TimeZone": str(self.handshake_timezone),
            "Realtime": str(self.handshake_realtime),
            "Encrypt": "0",
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
