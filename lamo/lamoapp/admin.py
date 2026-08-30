from django.contrib import admin
from .models import CustomUser, UserProfile, Service, WorkerProfile, Booking, Notification, Complaint, Review, WorkerLeave


@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    list_display = ('username', 'email', 'role', 'is_email_verified', 'is_staff', 'is_active', 'date_joined')
    list_filter = ('role', 'is_email_verified', 'is_staff', 'is_active')
    search_fields = ('username', 'email')
    actions = ['verify_email']

    @admin.action(description='Mark selected users as email verified')
    def verify_email(self, request, queryset):
        queryset.update(is_email_verified=True)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'phone', 'district', 'updated_at')
    search_fields = ('user__username', 'phone', 'district')
    list_filter = ('district',)


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ('name', 'price', 'is_active', 'is_featured', 'created_at')
    list_filter = ('is_active', 'is_featured')
    search_fields = ('name', 'description')


@admin.register(WorkerProfile)
class WorkerProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'service', 'phone', 'district', 'pricing_type', 'pricing_amount', 'work_status', 'is_verified')
    list_filter = ('work_status', 'is_verified', 'district', 'service', 'pricing_type')
    search_fields = ('user__username', 'phone', 'district')


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer', 'worker', 'service', 'status', 'date', 'estimated_cost', 'created_at')
    list_filter = ('status', 'booking_type', 'date')
    search_fields = ('customer__username', 'worker__user__username', 'service__name')


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('recipient', 'notification_type', 'message', 'is_read', 'created_at')
    list_filter = ('notification_type', 'is_read')


@admin.register(Complaint)
class ComplaintAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer', 'booking', 'subject', 'severity', 'status', 'created_at')
    list_filter = ('status', 'severity')
    search_fields = ('subject', 'description', 'customer__username')


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer', 'worker', 'rating', 'created_at')
    list_filter = ('rating',)


@admin.register(WorkerLeave)
class WorkerLeaveAdmin(admin.ModelAdmin):
    list_display = ('worker', 'leave_date', 'reason', 'created_at')
    list_filter = ('leave_date',)
    search_fields = ('worker__user__username', 'reason')
