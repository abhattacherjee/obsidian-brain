"""One doctor repair scope shared by both legacy package import forms."""
import contextvars

REPAIRS = contextvars.ContextVar("doctor_repairs", default=None)
