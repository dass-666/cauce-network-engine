"""E5 (sección I): causa demostrada del aviso de la CI de Windows. Un cliente que corta con RST a mitad de petición
no debe dejar una traza de excepción en el registro del motor; cualquier otra excepción, sí."""
import contextlib
import io
import socket
import struct
import time
import unittest

from helpers import sim_engine
from cauce.api import CauceAPI, _Server


class DesconexionClienteTest(unittest.TestCase):
    def test_rst_del_cliente_no_deja_traza(self):
        eng, _ = sim_engine()
        api = CauceAPI(eng, "127.0.0.1", 0)
        api.start()
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                for _ in range(3):
                    s = socket.create_connection(("127.0.0.1", api.port))
                    s.sendall(b"GET /api/v1/ping HTTP/1.1\r\nHost: x\r\n")
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    s.close()
                time.sleep(1.0)
        finally:
            api.stop()
        self.assertEqual(err.getvalue().count("Traceback"), 0, err.getvalue())

    def test_otra_excepcion_sigue_con_traza(self):
        srv = _Server.__new__(_Server)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                raise ValueError("fallo de verdad")
            except ValueError:
                srv.handle_error(None, ("127.0.0.1", 1))
        self.assertIn("Traceback", err.getvalue())
        self.assertIn("ValueError", err.getvalue())


if __name__ == "__main__":
    unittest.main()
