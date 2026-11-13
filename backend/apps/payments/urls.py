from django.urls import path

from .views import (
    PaymentListView,
    InitializePaymentView,
    VerifyPaymentView,
    PaymentDetailView,
    PaystackWebhookView,
    UnverifiedPaymentsView,
    PaymentProofView,
    SubmitOfflinePaymentView,
    PendingPaymentsView,
    ReviewPaymentView,
)


urlpatterns = [
    path('history/', PaymentListView.as_view(), name='payment-history'),
    path('initiate/', InitializePaymentView.as_view(), name='payment-initiate'),
    path('verify/<str:reference>/', VerifyPaymentView.as_view(), name='payment-verify'),
    path('<int:pk>/receipt/', PaymentDetailView.as_view(), name='payment-receipt'),
    path('webhook/', PaystackWebhookView.as_view(), name='paystack-webhook'),
    path('unverified/', UnverifiedPaymentsView.as_view(), name='payment-unverified'),

    # Offline self-reported payments: student submits proof, rep/admin reviews.
    path('submit/', SubmitOfflinePaymentView.as_view(), name='payment-submit'),
    path('pending/', PendingPaymentsView.as_view(), name='payment-pending'),
    path('<int:pk>/review/', ReviewPaymentView.as_view(), name='payment-review'),
    path('<int:pk>/proof/', PaymentProofView.as_view(), name='payment-proof'),
]