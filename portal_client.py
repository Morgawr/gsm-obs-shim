
import sys
import dbus
import dbus.mainloop.glib
from gi.repository import GLib

# dbus-python requires a main loop for signals
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

class PortalClient:
    def __init__(self):
        self.bus = dbus.SessionBus()
        self.portal_obj = self.bus.get_object('org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop')
        self.portal_iface = dbus.Interface(self.portal_obj, 'org.freedesktop.portal.ScreenCast')
        self.loop = GLib.MainLoop()
        self.response_data = None
        self.response_error = None

    def _response_handler(self, response, results, path=None):
        # response: uint32 (0=success, 1=cancelled, 2=ended)
        # results: a{sv}
        if response == 0:
            self.response_data = results
        elif response == 1:
            self.response_error = "User cancelled interaction"
        else:
            self.response_error = f"Request failed with code {response}"
        self.loop.quit()

    def _wait_for_response(self, request_path):
        # Add signal receiver for this specific request path
        # Note: The sender of the signal is the portal service.
        # The path is the request object path.
        # Interface is org.freedesktop.portal.Request
        
        match = self.bus.add_signal_receiver(
            self._response_handler,
            signal_name='Response',
            dbus_interface='org.freedesktop.portal.Request',
            bus_name='org.freedesktop.portal.Desktop',
            path=request_path
        )
        
        self.response_data = None
        self.response_error = None
        
        try:
            self.loop.run()
        finally:
            # Clean up signal receiver? dbus-python match objects support remove()
            match.remove()
            
        if self.response_error:
            raise Exception(self.response_error)
        return self.response_data

    def create_session(self):
        # options: a{sv}
        # dbus-python automatically marshals python dicts to a{sv} if types match or are messy
        # explicit typing using dbus.Dictionary/String helps
        
        options = dbus.Dictionary({
            'session_handle_token': dbus.String('gsm_obs_session'),
            'handle_token': dbus.String('gsm_obs_session_req')
        }, signature='sv')
        
        request_path = self.portal_iface.CreateSession(options)
        
        results = self._wait_for_response(request_path)
        return results['session_handle']

    def select_sources(self, session_handle):
        options = dbus.Dictionary({
            'handle_token': dbus.String('gsm_obs_source_req'),
            'types': dbus.UInt32(3), # 1 (Monitor) | 2 (Window)
            'cursor_mode': dbus.UInt32(2), # Embedded
            'multiple': dbus.Boolean(False)
        }, signature='sv')
        
        request_path = self.portal_iface.SelectSources(session_handle, options)
        self._wait_for_response(request_path)

    def start(self, session_handle):
        options = dbus.Dictionary({
            'handle_token': dbus.String('gsm_obs_start_req')
        }, signature='sv')
        
        request_path = self.portal_iface.Start(session_handle, '', options)
        results = self._wait_for_response(request_path)
        
        streams = results.get('streams')
        if not streams:
             raise Exception("No streams returned")
             
        # streams is a(ua{sv})
        # streams[0] is (node_id, properties)
        node_id = streams[0][0]
        return int(node_id)

def main():
    try:
        print("Initializing PortalClient...", file=sys.stderr)
        client = PortalClient()
        
        print("Creating session...", file=sys.stderr)
        session_handle = client.create_session()
        print(f"Session Handle: {session_handle}", file=sys.stderr)
        
        print("Selecting sources...", file=sys.stderr)
        client.select_sources(session_handle)
        
        print("Starting session...", file=sys.stderr)
        node_id = client.start(session_handle)
        print(f"Got Node ID: {node_id}", file=sys.stderr)
        
        print(node_id) # Output ONLY the node_id to stdout for parsing
        sys.stdout.flush()
        
        # Keep session alive
        while True:
            import time
            time.sleep(1)
            
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
