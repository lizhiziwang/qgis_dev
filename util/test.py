# from sm_bc.crypto.signers import SM2Signer
# from sm_bc.crypto.params.ec_public_key_parameters import  ECPublicKeyParameters
# from sm_bc.crypto.params.ec_private_key_parameters import ECPrivateKeyParameters
# from sm_bc.math.ec_curve import Fp
# import secrets
#
# # 生成密钥对
# curve = Fp()
# d = secrets.randbelow(curve.n)  # 私钥
# public_key = curve.G.multiply(d)  # 公钥
#
# # 创建签名器
# signer = SM2Signer()
#
# # 签名消息
# message = b"Message to sign"
# priv_params = ECPrivateKeyParameters(d, curve.domain_params)
# signer.init(True, priv_params)
# signature = signer.generate_signature(message)
#
# # 验证签名
# pub_params = ECPublicKeyParameters(public_key, curve.domain_params)
# signer.init(False, pub_params)
# is_valid = signer.verify_signature(message, signature)
#
# print(f"签名有效: {is_valid}")