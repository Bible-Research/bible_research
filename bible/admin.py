from django.contrib import admin

from bible.models import FeatureFlag
from bible.services.feature_flags import invalidate_feature_flag


@admin.register(FeatureFlag)
class FeatureFlagAdmin(admin.ModelAdmin):
    """Feature flags are runtime kill switches — toggling one
    must bust the read-through cache immediately so the new
    value takes effect without waiting out the TTL."""
    list_display = ('key', 'enabled', 'description', 'updated_at')
    readonly_fields = ('updated_at',)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        invalidate_feature_flag(obj.key)

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        invalidate_feature_flag(obj.key)

    def delete_queryset(self, request, queryset):
        # The bulk "Delete selected" action bypasses
        # ``delete_model`` — bust each flag's cached value
        # here too or a bulk-deleted flag stays cached for
        # up to the TTL.
        keys = list(queryset.values_list('key', flat=True))
        super().delete_queryset(request, queryset)
        for key in keys:
            invalidate_feature_flag(key)
