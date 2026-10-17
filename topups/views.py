from django.contrib.auth import logout
from rest_framework import status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    SimProfile,
    TopupTransaction,
    TransactionStatus,
)
from .serializers import (
    EmailLoginSerializer,
    SimProfileSerializer,
    TopupRequestSerializer,
    TopupTransactionSerializer,
    UserSerializer,
)
from .services import execute_topup


class LoginView(APIView):
    permission_classes = []
    authentication_classes = []
    throttle_scope = "login"
    serializer_class = EmailLoginSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.serializer_class(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)

        user = serializer.validated_data["user"]
        token, _ = Token.objects.get_or_create(user=user)

        return Response(
            {
                "token": token.key,
                "user": UserSerializer(user).data,
            }
        )


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class SimProfileViewSet(viewsets.ModelViewSet):
    serializer_class = SimProfileSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = SimProfile.objects.select_related("owner")

        if self.request.user.is_staff:
            return queryset

        return queryset.filter(owner=self.request.user)


class TopupTransactionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TopupTransactionSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = []

    def get_queryset(self):
        queryset = TopupTransaction.objects.select_related(
            "user",
            "sim_profile",
        )

        if self.request.user.is_staff:
            return queryset

        return queryset.filter(user=self.request.user)

    @action(
        detail=False,
        methods=["post"],
        url_path="send",
    )
    def send(self, request):
        serializer = TopupRequestSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)

        transaction, created = execute_topup(
            request.user,
            serializer.validated_data,
        )

        response_status = (
            status.HTTP_201_CREATED
            if created
            else status.HTTP_200_OK
        )

        return Response(
            TopupTransactionSerializer(transaction).data,
            status=response_status,
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="retry",
    )
    def retry(self, request, pk=None):
        transaction = self.get_object()

        if transaction.status != TransactionStatus.FAILED:
            return Response(
                {
                    "error": (
                        "Only failed transactions can be retried."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not transaction.idempotency_key:
            return Response(
                {
                    "error": (
                        "This transaction has no idempotency key "
                        "and cannot be retried safely."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        transaction, _ = execute_topup(
            transaction.user,
            {
                "sim_profile": transaction.sim_profile,
                "product": transaction.product,
                "phone_number": transaction.phone_number,
                "amount": transaction.amount,
                "idempotency_key": transaction.idempotency_key,
                "retry_failed": True,
            },
        )

        return Response(
            TopupTransactionSerializer(transaction).data,
            status=status.HTTP_200_OK,
        )