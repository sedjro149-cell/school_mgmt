from django.contrib import admin
from .models import FeeType, FeeTypeAmount, Fee, Payment


class FeeTypeAmountInline(admin.TabularInline):
    model = FeeTypeAmount
    extra = 1
    # autocomplete_fields removed because Level admin is not registered for autocomplete
    fields = ["level", "amount", "is_active"]
    show_change_link = True


@admin.register(FeeType)
class FeeTypeAdmin(admin.ModelAdmin):
    list_display = ["name", "is_active", "created_at", "levels_display"]
    search_fields = ["name"]
    inlines = [FeeTypeAmountInline]

    def levels_display(self, obj):
        return ", ".join([f"{fta.level.name} ({fta.amount})" for fta in obj.amounts.all()])
    levels_display.short_description = "Niveaux (montant)"


@admin.register(FeeTypeAmount)
class FeeTypeAmountAdmin(admin.ModelAdmin):
    list_display = ["fee_type", "level", "amount", "is_active"]
    list_filter = ["level", "fee_type"]
    search_fields = ["fee_type__name", "level__name"]


@admin.register(Fee)
class FeeAdmin(admin.ModelAdmin):
    list_display = ["student", "fee_type", "get_level", "amount", "paid", "payment_date", "created_at"]
    list_filter = ["fee_type", "paid"]
    search_fields = ["student__first_name", "student__last_name", "fee_type__name"]

    def get_level(self, obj):
        lvl = getattr(obj, "level", None)
        return lvl.name if lvl else "-"
    get_level.short_description = "Level"


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ["id", "fee", "amount", "validated", "validated_by", "paid_at"]
    list_filter = ["validated", "paid_at"]
    search_fields = ["fee__student__first_name", "fee__student__last_name", "reference"]
