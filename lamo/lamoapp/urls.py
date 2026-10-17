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
    path('reset-password/<str:token>/', views.forgot_password_view, name='reset_password'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('admin-dashboard/', views.admin_dashboard, name='admin_dashboard'),
    path('user-dashboard/', views.user_dashboard, name='user_dashboard'),
    path('worker-dashboard/', views.worker_dashboard, name='worker_dashboard'),
    path('about/', views.about, name='about'),
    path('contact/', views.contact, name='contact'),
    path('terms/', views.terms_view, name='terms'),
    path('privacy/', views.privacy_view, name='privacy'),
    path('splash/', views.splash_view, name='splash'),
    path('robots.txt', views.robots_txt, name='robots_txt'),
    path('sitemap.xml', views.sitemap_xml, name='sitemap_xml'),

    # Profile Management
    path('profile/edit/', views.edit_profile_view, name='edit_profile'),
    path('profile/delete/', views.delete_account_view, name='delete_account'),
    path('profile/change-password/', views.change_password_view, name='change_password'),
    path('worker/profile/edit/', views.edit_worker_profile_view, name='edit_worker_profile'),

    # Complaints
    path('my-complaints/', views.my_complaints_view, name='my_complaints'),

    # Booking Management
    path('book-service/', views.book_service, name='book_service'),
    path('my-bookings/', views.my_bookings, name='my_bookings'),
    path('booking/<int:booking_id>/cancel/', views.cancel_booking, name='cancel_booking'),
    path('booking/<int:booking_id>/confirm-payment/', views.confirm_payment, name='confirm_payment'),
    path('booking/<int:booking_id>/messages/', views.booking_messages, name='booking_messages'),
    path('booking/<int:booking_id>/confirm-completion/', views.confirm_completion, name='confirm_completion'),
    path('booking/<int:booking_id>/dispute/', views.dispute_booking, name='dispute_booking'),
    path('booking/<int:booking_id>/report-no-show/', views.report_worker_no_show, name='report_worker_no_show'),
    # path('booking/<int:booking_id>/complaint/', views.raise_complaint, name='raise_complaint'),
    path('booking/<int:booking_id>/review/', views.submit_review, name='submit_review'),

    # Invoices / Receipts
    path('booking/<int:booking_id>/invoice/', views.booking_invoice, name='booking_invoice'),

    # Recurring Booking Series
    path('my-series/', views.my_series, name='my_series'),
    path('my-series/<int:series_id>/toggle/', views.series_toggle_active, name='series_toggle_active'),
    path('my-series/<int:series_id>/cancel/', views.series_cancel, name='series_cancel'),

    # Service Management
    path('services/add/', views.service_create, name='service_create'),
    path('services/edit/<int:pk>/', views.service_update, name='service_update'),
    path('services/delete/<int:pk>/', views.service_delete, name='service_delete'),
    path('service_list/', views.service_list, name='service_list'),
    path('services/<int:pk>/', views.service_detail, name='service_detail'),
    path('services/<int:pk>/workers/', views.service_workers, name='service_workers'),
    path('service_page/', views.service_page, name='service_page'),

    # Service Request Management (Custom "Others" workflow)
    path('service-requests/', views.service_requests_list, name='service_requests'),
    path('service-requests/<int:pk>/approve/', views.service_request_approve, name='service_request_approve'),
    path('service-requests/<int:pk>/publish/', views.service_request_publish, name='service_request_publish'),

    # Worker Management & Actions
    path('users/', views.users_list, name='users_list'),
    path('users/<int:user_id>/delete/', views.admin_delete_user, name='admin_delete_user'),
    path('workers/', views.workers_list, name='workers_list'),
    path('workers/<int:worker_id>/delete/', views.admin_delete_worker, name='admin_delete_worker'),
    path('workers/<int:worker_id>/suspend/', views.admin_suspend_worker, name='admin_suspend_worker'),
    path('workers/<int:worker_id>/strike/', views.admin_worker_strike, name='admin_worker_strike'),
    path('workers/verify/<int:pk>/', views.worker_verify, name='worker_verify'),
    path('worker/work-dashboard/', views.worker_manage_dashboard, name='worker_manage_dashboard'),
    path('worker-jobs/', views.worker_jobs, name='worker_jobs'),
    path('worker-earnings/', views.worker_earnings, name='worker_earnings'),
    path('worker-profile/', views.worker_profile, name='worker_profile'),
    path('worker/wallet/recharge/', views.worker_recharge_wallet, name='worker_recharge_wallet'),
    path('worker-notifications/', views.worker_notifications, name='worker_notifications'),
    path('worker/update-status/', views.worker_update_status, name='worker_update_status'),

    # Worker Booking Lifecycle
    path('worker/bookings/<int:booking_id>/accept/', views.worker_accept_booking, name='worker_accept_booking'),
    path('worker/bookings/<int:booking_id>/reject/', views.worker_reject_booking, name='worker_reject_booking'),
    path('worker/booking/<int:booking_id>/start/', views.worker_start_job, name='worker_start_job'),
    path('worker/jobs/<int:booking_id>/start/', views.worker_start_job, name='worker_start_job_alias'),
    path('worker/booking/<int:booking_id>/request-completion/', views.worker_request_completion, name='worker_request_completion'),
    path('worker/jobs/<int:booking_id>/request-completion/', views.worker_request_completion, name='worker_request_completion_alias'),
    path('worker/booking/<int:booking_id>/cancel/', views.worker_cancel_booking, name='worker_cancel_booking'),
    path('worker/jobs/<int:booking_id>/cancel/', views.worker_cancel_booking, name='worker_cancel_booking_alias'),
    path('worker/booking/<int:booking_id>/report-no-show/', views.worker_report_customer_no_show, name='worker_report_customer_no_show'),
    path('worker/jobs/<int:booking_id>/report-customer-no-show/', views.worker_report_customer_no_show, name='worker_report_customer_no_show_alias'),
    path('worker/emergency/<int:booking_id>/accept/', views.worker_accept_emergency, name='worker_accept_emergency'),

    # Notifications
    path('notifications/', views.notifications_view, name='notifications'),
    path('notifications/<int:notification_id>/mark-read/', views.mark_notification_read, name='mark_notification_read'),
    path('notifications/mark-all-read/', views.mark_all_notifications_read, name='mark_all_notifications_read'),
    path('admin-notifications/', views.admin_notifications, name='admin_notifications'),
    path('admin-dashboard/broadcast/', views.admin_broadcast_announcement, name='admin_broadcast_announcement'),
    path('admin-dashboard/bookings/', views.admin_booking_details, name='admin_booking_details'),
    path('admin-dashboard/payments/', views.admin_payments, name='admin_payments'),
    path(
        'admin-dashboard/payments/<int:payment_id>/confirm/',
        views.admin_confirm_payment,
        name='admin_confirm_payment',
    ),
    path('admin-dashboard/bookings/<int:booking_id>/', views.admin_booking_view, name='admin_booking_view'),
    path('admin-dashboard/reviews-complaints/', views.admin_reviews_complaints, name='admin_reviews_complaints'),
    path('admin-dashboard/contact-enquiries/', views.admin_contact_enquiries, name='admin_contact_enquiries'),
    path('admin-dashboard/enquiry/<int:enquiry_id>/', views.admin_enquiry_detail, name='admin_enquiry_detail'),
    path('complaint/<int:complaint_id>/update-status/', views.update_complaint_status, name='update_complaint_status'),

    # CSV Exports
    path('admin-export/<str:dataset>/', views.admin_export_csv, name='admin_export_csv'),

    # Worker Proof Document (private, admin-only download)
    path('workers/<int:pk>/proof-document/', views.proof_download, name='worker_proof_document'),

    # AI Feature
    path('ai/assistant/', views.ai_service_assistant, name='ai_service_assistant'),
    path('services/<int:service_id>/ai-recommend-workers/', views.ai_recommend_workers, name='ai_recommend_workers'),
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