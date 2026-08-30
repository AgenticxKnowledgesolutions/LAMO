from django.urls import path
from . import views

urlpatterns = [
    # General / Dashboard
    path('', views.home, name='home'),
    path('register/', views.register_view, name='register'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('verify-otp/', views.verify_otp_view, name='verify_otp'),
    path('forgot-password/', views.forgot_password_view, name='forgot_password'),
    path('reset-password/<str:token>/', views.reset_password_view, name='reset_password'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('admin-dashboard/', views.admin_dashboard, name='admin_dashboard'),
    path('user-dashboard/', views.user_dashboard, name='user_dashboard'),
    path('worker-dashboard/', views.worker_dashboard, name='worker_dashboard'),
    path('about/', views.about, name='about'),
    path('contact/', views.contact, name='contact'),

    # Profile Management
    path('profile/edit/', views.edit_profile_view, name='edit_profile'),
    path('profile/delete/', views.delete_account_view, name='delete_account'),
    path('worker/profile/edit/', views.edit_worker_profile_view, name='edit_worker_profile'),

    # Complaints
    path('my-complaints/', views.my_complaints_view, name='my_complaints'),

    # Booking Management
    path('book-service/', views.book_service, name='book_service'),
    path('my-bookings/', views.my_bookings, name='my_bookings'),
    path('booking/<int:booking_id>/cancel/', views.cancel_booking, name='cancel_booking'),
    path('booking/<int:booking_id>/confirm-completion/', views.confirm_completion, name='confirm_completion'),
    # path('booking/<int:booking_id>/complaint/', views.raise_complaint, name='raise_complaint'),
    path('booking/<int:booking_id>/review/', views.submit_review, name='submit_review'),

    # Service Management
    path('services/add/', views.service_create, name='service_create'),
    path('services/edit/<int:pk>/', views.service_update, name='service_update'),
    path('services/delete/<int:pk>/', views.service_delete, name='service_delete'),
    path('service_list/', views.service_list, name='service_list'),
    path('services/<int:pk>/', views.service_detail, name='service_detail'),
    path('services/<int:pk>/workers/', views.service_workers, name='service_workers'),
    path('service_page/', views.service_page, name='service_page'),

    # Worker Management & Actions
    path('users/', views.users_list, name='users_list'),
    path('workers/', views.workers_list, name='workers_list'),
    path('workers/verify/<int:pk>/', views.worker_verify, name='worker_verify'),
    path('worker/work-dashboard/', views.worker_manage_dashboard, name='worker_manage_dashboard'),
    path('worker-jobs/', views.worker_jobs, name='worker_jobs'),
    path('worker-profile/', views.worker_profile, name='worker_profile'),
    path('worker-notifications/', views.worker_notifications, name='worker_notifications'),
    path('worker/update-status/', views.worker_update_status, name='worker_update_status'),

    # Worker Booking Lifecycle
    path('worker/bookings/<int:booking_id>/accept/', views.worker_accept_booking, name='worker_accept_booking'),
    path('worker/bookings/<int:booking_id>/reject/', views.worker_reject_booking, name='worker_reject_booking'),
    path('worker/booking/<int:booking_id>/start/', views.worker_start_job, name='worker_start_job'),
    path('worker/booking/<int:booking_id>/request-completion/', views.worker_request_completion, name='worker_request_completion'),

    # Admin Management
    path('notifications/', views.notifications_view, name='notifications'),
    path('admin-notifications/', views.admin_notifications, name='admin_notifications'),
    path('admin-dashboard/bookings/', views.admin_booking_details, name='admin_booking_details'),
    path('admin-dashboard/bookings/<int:booking_id>/', views.admin_booking_view, name='admin_booking_view'),
    path('admin-dashboard/reviews-complaints/', views.admin_reviews_complaints, name='admin_reviews_complaints'),
    path('complaint/<int:complaint_id>/update-status/', views.update_complaint_status, name='update_complaint_status'),

    # AI Feature
    path('ai/assistant/', views.ai_service_assistant, name='ai_service_assistant'),
    path(
        'booking/<int:booking_id>/complaint/',
        views.raise_complaint,
        name='raise_complaint'
    ),
    path(
        'booking/<int:booking_id>/feedback/',
        views.submit_feedback,
        name='submit_feedback'
    ),

    # Worker Leave Management
    path(
        'worker/leave/',
        views.worker_leave_management,
        name='worker_leave'
    ),
    path(
        'worker/leave/<int:leave_id>/delete/',
        views.worker_leave_delete,
        name='worker_leave_delete'
    ),
]