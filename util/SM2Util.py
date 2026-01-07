# import base64
# import binascii
# import os
# import struct
# from pathlib import Path
#
# from gmssl import sm2, func, sm4
# # from component.file import *
# #16进制的公钥和私钥
#
# sm4_key = "123456"
#
# key = {'private_key': '6d289b00876e5315dddc09590ed1eea3be0cf633b4d194981d77f52a293edba9'
# , 'public_key': '1ca287f4369bc81c1a8047b36da163c34160b7deddb8545c05a06ef4d7a230f597096b7f0212d491e38a25f596dbbde3cc32e0f2b12c4a3c58324a9cfb486cbd'}
#
#
# def create_key():
#     private_key = func.random_hex(64)
#     public_key = get_public_key(private_key)
#     return {
#         "private_key": private_key,
#         "public_key": public_key,
#     }
#
# def get_public_key(private_key):
#     Gx = '32C4AE2C1F1981195F9904466A39C9948FE30BBFF2660BE1715A4589334C74C7'
#     Gy = 'BC3736A2F4F6779C59BDCEE36B692153D0A9877CC62A474002DF32E52139F0A0'
#     G = Gx + Gy  # ⚠️ 必须是字符串拼接，不是元组！
#
#     # 3. 创建临时实例用于计算公钥
#     temp_sm2 = sm2.CryptSM2(public_key='', private_key='')
#
#     # 4. 计算公钥 = private_key * G
#     k = int(private_key, 16)
#     return temp_sm2._kg(k, G)
#
# def encrypt_str(plaintext, public_key) -> bytes:
#     sm2_crypt = sm2.CryptSM2(public_key=public_key, private_key='')
#
#     data = plaintext.encode('utf-8')
#     data = sm2_crypt.encrypt(data)
#     return data
# def _encrypt_bytes(data, public_key) -> bytes:
#     sm2_crypt = sm2.CryptSM2(public_key=public_key, private_key='')
#
#     data = sm2_crypt.encrypt(data)
#     return data
#
# def decrypt_str(ciphertext: str, private_key):
#     sm2_crypt = sm2.CryptSM2(public_key='', private_key=private_key)
#     data = ciphertext.encode('utf-8')
#     data = sm2_crypt.decrypt(data)
#     print(data)
#     return data
#
# def _decrypt_byte(data: bytes, private_key):
#     sm2_crypt = sm2.CryptSM2(public_key='', private_key=private_key)
#     data = sm2_crypt.decrypt(data)
#     print(data)
#     return data
#
# """
#     用 SM2 加密对称密钥，再用对称密钥（如 SM4）加密文件。
# """
#
#
# def encrypt_file(file_path, public_key=None) -> bool:
#     # 修复默认参数陷阱
#     if public_key is None:
#         public_key = key["public_key"]
#
#     try:
#         # 优化路径处理
#         CHUNK_SIZE = 16384
#         file_path_obj = Path(file_path)
#         target_filename = file_path_obj.parent / (file_path_obj.name + ".sm")
#
#         sm4_key = "1234567890abcdef"  # 16字节合法SM4密钥
#         iv = b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
#
#         # 公钥加密SM4密钥
#         e_key = encrypt_str(sm4_key, public_key)
#
#         # 初始化SM4加密器（CBC模式）
#         sm4_crypt = sm4.CryptSM4()
#         sm4_crypt.set_key(key=e_key, mode=sm4.SM4_ENCRYPT)
#
#         # 流式分块处理：读取-加密-写入
#         with open(file_path, "rb") as in_file, open(target_filename, "wb") as out_file:
#             # 循环分块读取文件
#             while True:
#                 # 每次读取CHUNK_SIZE字节的数据
#                 chunk_data = in_file.read(CHUNK_SIZE)
#                 # 读取到文件末尾，退出循环
#                 if not chunk_data:
#                     break
#
#                 # 对当前块进行加密（CBC模式自动维护加密状态）
#                 encrypted_chunk = sm4_crypt.crypt_cbc(iv, chunk_data)
#                 # 立即写入加密后的块，释放内存
#                 out_file.write(encrypted_chunk)
#
#                 # 注意：CBC模式下，下一块的IV需要更新为当前块的加密结果最后16字节
#                 # 若使用的sm4库不自动维护IV，需手动更新：iv = encrypted_chunk[-16:]
#
#         print(f"分块加密完成！加密后文件路径：{target_filename}")
#         return True
#     except FileNotFoundError as e:
#         print(f"加密失败：未找到指定文件 - {e}")
#         return False
#     except Exception as e:
#         print(f"加密失败：未知错误 - {e}")
#         return False
#
# def decrypt_file(file_path, private_key) -> bool:
#     try:
#         data = None
#         filename = Path(file_path).parent / Path(file_path).name[:-3]
#         with open(file_path, "rb") as f:
#             data = f.read()
#         data = _decrypt_byte(data, private_key)
#
#         with open(filename, "wb") as f:
#             f.write(data)
#         return True
#     except FileNotFoundError as f:
#         print(f)
#         return False
#
#
# if __name__ == '__main__':
#     # key = create_key()
#     # sm2_crypt = sm2.CryptSM2(
#     #     public_key=key["public_key"], private_key=key["private_key"])
#     # print("公钥:", sm2_crypt.public_key)
#     #
#     # data = "你好"
#     # data = data.encode('utf-8')
#     # enc_data = sm2_crypt.encrypt(data)
#     # dec_data = sm2_crypt.decrypt(enc_data)
#     # print("\n")
#     # print(enc_data)
#     # print(dec_data.decode('utf-8'))
#
#     path = r"D:\zsh\temp\123.zip"
#     encrypt_file(path)
