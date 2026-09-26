"""算力券核销服务向 API 和 CLI 暴露的稳定错误。"""


class VoucherError(RuntimeError):
    code = "voucher_error"
    status = 400


class NotFound(VoucherError):
    code = "not_found"
    status = 404


class Conflict(VoucherError):
    code = "conflict"
    status = 409


class Forbidden(VoucherError):
    code = "forbidden"
    status = 403


class InvalidState(VoucherError):
    code = "invalid_state"
    status = 409


class ValidationFailed(VoucherError):
    code = "validation_failed"
    status = 422
