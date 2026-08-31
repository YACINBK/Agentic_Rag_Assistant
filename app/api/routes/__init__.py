from app.api.routes.admin import admin_router
from app.api.routes.images import router as images_router
from app.api.routes.onboarding import onboarding_router
from app.api.routes.search import search_router

__all__ = ["admin_router", "images_router", "onboarding_router", "search_router"]
