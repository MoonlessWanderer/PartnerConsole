from fastapi import APIRouter

from app.api.admin import router as admin_router
from app.api.partner import router as partner_router

api = APIRouter()
api.include_router(admin_router)
api.include_router(partner_router)


@api.get("/health")
def health():
    return {"ok": True}
