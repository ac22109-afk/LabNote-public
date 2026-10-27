"""Windowed entry point plus a non-interactive packaging diagnostic."""
import json
import sys
from pathlib import Path


def self_check(report_path):
    import tempfile
    import time
    import sqlite3
    import tkinter as tk
    from .app import App, RecordDialog
    from .store import FileStore
    from .session import DesktopSession
    from .maintenance import MaintenanceDialog
    from . import runtime
    original_profile_directory = runtime.profile_directory

    report = {'ok': False, 'frozen': bool(getattr(sys, 'frozen', False)),
              'platform': sys.platform, 'console_streams_absent': sys.stdout is None and sys.stderr is None}
    if sys.platform == 'win32':
        import ctypes
        report['console_window_present'] = bool(ctypes.windll.kernel32.GetConsoleWindow())
    root = None
    try:
        with tempfile.TemporaryDirectory(prefix='labnote-package-check-') as temporary:
            runtime.profile_directory = lambda: Path(temporary) / 'profile'
            if getattr(sys, 'frozen', False):
                deployment = runtime.load_deployment()
                report.update(embedded_deployment=runtime.bundled_deployment_file().is_file(),
                              deployment_mode=deployment['mode'], deployment_id=deployment['store_id'],
                              deployment_imported=(runtime.profile_directory() / 'labnote-deployment.json').is_file())
                # Simulate moving the exe to a directory without its sidecar.
                previous_directory = runtime.deployment_directory
                try:
                    changed = runtime.save_deployment({**deployment, 'mac_path': '/Volumes/Verified/data'}, bound=deployment)
                    runtime.deployment_directory = lambda: Path(temporary) / 'moved-exe'
                    if runtime.load_deployment() != changed:
                        raise RuntimeError('Moving the app lost the imported settings.')
                finally:
                    runtime.deployment_directory = previous_directory
                report['import_survives_move'] = True
            store = FileStore.create(Path(temporary) / 'data')
            sample = store.save('sample', {'label': 'PACKAGE-DEMO', 'creator': 'zz00000', 'material': 'Al'}, 'zz00000')
            measurement = store.save('measurement', {'sample_id': sample['id'], 'measurer': 'zz00000',
                'method': 'SEM', 'metric': '結晶粒径', 'unit': 'μm', 'measured_on': '2026-10-19', 'values': ['1', '3', '5']}, 'zz00000')
            session = DesktopSession(store.root, store.store_id, cache_home=Path(temporary) / 'pc')
            session.load()
            queued = session.save('sample', {'label': 'OFFLINE-DEMO', 'creator': 'zz00002', 'material': 'アルミニウム合金', 'alloy': 'A1050'}, ' ＺＺ００００２ ')
            if len(session.pending()) != 1 or queued['registrant'] != 'zz00002':
                raise RuntimeError('Bundled offline queue or ID normalization failed.')
            root = tk.Tk()
            root.withdraw()
            app = App(root, session, production=True)
            if app.bound_store_id != store.store_id or '試用版' in root.title():
                raise RuntimeError('Production binding or window title is incorrect.')
            root.geometry('900x650')
            root.update()
            if any(button.winfo_rootx()+button.winfo_width() > root.winfo_rootx()+root.winfo_width() for button in app.feature_buttons):
                raise RuntimeError('Main import or curve buttons are clipped.')
            root.geometry('1180x780')
            if {item['name'] for item in app.font_status} != {'Arial', 'Times New Roman', 'MS 明朝', 'MS Pゴシック', 'Symbol'}:
                raise RuntimeError('Bundled font prerequisite manifest is incomplete.')
            errors = []
            root.report_callback_exception = lambda kind, value, trace: errors.append(str(value))
            deadline = time.monotonic() + 10
            while app.job is not None and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            if app.snapshot is None or len(app.samples.get_children()) != 2:
                raise RuntimeError('Bundled UI did not load synthetic records.')
            if app.actor.get() or app.actor_hint.cget('text') != '例：zz00001':
                raise RuntimeError('ID hint must not become an entered ID.')
            for kind, existing, parent in [('sample', None, None), ('sample', sample, None),
                    ('measurement', None, sample), ('measurement', measurement, sample), ('treatment', None, sample)]:
                dialog = RecordDialog(app, kind, existing, parent)
                if kind == 'sample' and existing is None:
                    if str(dialog.fields['material'].cget('state')) != 'readonly':
                        raise RuntimeError('Material field must use controlled choices.')
                    dialog.fields['material'].set('アルミニウム合金')
                    dialog.update_choices('material')
                    dialog.fields['alloy'].set('A5052')
                    dialog.fields['material'].set('マグネシウム合金')
                    dialog.update_choices('material')
                    if dialog.fields['alloy'].get() or 'A5052' in dialog.fields['alloy'].cget('values'):
                        raise RuntimeError('Changing material must clear the old alloy.')
                dialog.withdraw()
                dialog.grab_release()
                dialog.destroy()
            settings = MaintenanceDialog(app)
            if settings.enabled.get() or str(settings.open_button.cget('state')) != 'disabled' or str(settings.save_button.cget('state')) != 'disabled':
                raise RuntimeError('Storage changes must require an explicit maintenance action.')
            settings.withdraw()
            settings.grab_release()
            settings.destroy()
            app.actor.set('zz00000')
            sample_key = sample['id'] + ':' + sample['revision_id']
            app.samples.selection_set(sample_key)
            root.update()
            measurement_key = measurement['id'] + ':' + measurement['revision_id']
            app.measurements.selection_set(measurement_key)
            root.update()
            app.render_samples()
            root.update()
            if app.samples.selection() != (sample_key,) or app.measurements.selection() != (measurement_key,):
                raise RuntimeError('Reloading a list lost the selected sample or measurement.')

            def wait_job():
                deadline = time.monotonic() + 15
                while app.job is not None and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.01)
                root.update()
                if app.job is not None:
                    raise RuntimeError('Bundled form save did not finish.')

            unrelated = FileStore.create(Path(temporary) / 'other-target')
            rejected = []
            previous_error = app.show_error
            app.show_error = lambda exc: rejected.append(str(exc))
            app.change_store(lambda: unrelated)
            wait_job()
            app.show_error = previous_error
            if not rejected or app.store is not session:
                raise RuntimeError('Production UI allowed switching to an unrelated database.')

            # Exercise actual form save callbacks, including revealing a new
            # sample hidden by the previous search, without opening file dialogs.
            app.query.set('PACKAGE-DEMO')
            app.render_samples()
            dialog = RecordDialog(app, 'sample')
            dialog.fields['label'].insert(0, 'FORM-DEMO')
            dialog.fields['material'].set('アルミニウム合金')
            dialog.update_choices('material')
            dialog.fields['alloy'].set('A5052')
            dialog.save()
            wait_job()
            created = next(i for i in app.snapshot.of_kind('sample') if i['data']['label'] == 'FORM-DEMO')
            if app.query.get() or not app.samples.selection() or app.sample_items[app.samples.selection()[0]]['id'] != created['id']:
                raise RuntimeError('The newly saved sample was hidden by search or lost selection.')
            dialog = RecordDialog(app, 'measurement', sample=created)
            dialog.fields['method'].set('画像観察')
            dialog.update_choices('method')
            import base64
            fixture = Path(temporary) / '画像.PNG'
            fixture.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG1sAAAAASUVORK5CYII='))
            dialog.files = [str(fixture)]
            dialog.save()
            wait_job()
            if not app.measurements.selection() or app.pending_count != 3:
                raise RuntimeError('The newly saved measurement was not selected or queued. '
                    f'selection={app.measurements.selection()}, pending={app.pending_count}, errors={errors}')
            attachment_key = app.attachments.get_children()[0]
            app.attachments.selection_set(attachment_key)
            if app.attachments.item(attachment_key, 'values')[1] != '.PNG':
                raise RuntimeError('Original attachment extension was not displayed.')
            from .app import filedialog, messagebox
            original_dialog, original_info = filedialog.asksaveasfilename, messagebox.showinfo
            exported = Path(temporary) / '取り出した画像.PNG'
            try:
                filedialog.asksaveasfilename = lambda **kwargs: str(exported.with_suffix(''))
                messagebox.showinfo = lambda *args, **kwargs: None
                app.copy_attachment()
                wait_job()
                if exported.read_bytes() != fixture.read_bytes():
                    raise RuntimeError('Attachment export changed PNG data or lost its extension.')
            finally:
                filedialog.asksaveasfilename, messagebox.showinfo = original_dialog, original_info
            app.query.set('A5052')
            app.render_samples()
            root.update()
            if len(app.samples.get_children()) != 1:
                raise RuntimeError('Bundled SQLite search returned unexpected results.')
            from .comparison_ui import ComparisonDialog
            from .comparison import KINDS, export_comparison, export_svg
            root.title('LabNote — 配布確認（架空データ）')
            root.deiconify()
            root.update()
            compare = ComparisonDialog(app, style_file=Path(temporary) / 'graph-style.json',
                                       preset_file=Path(temporary) / 'figure-presets.json')
            root.update()
            compare.geometry('950x740')
            root.update()
            if compare.svg_button.winfo_rooty() + compare.svg_button.winfo_height() > compare.winfo_rooty() + compare.winfo_height():
                raise RuntimeError('Comparison export controls were clipped at the minimum screen size.')
            if len(compare.selected_rows) != 1 or len(compare.table.get_children()) != 1:
                raise RuntimeError('Comparison screen must show numeric measurements only.')
            for kind in KINDS:
                compare.kind.set(kind)
                compare.error_mode.set('標本SD')
                compare.refresh()
                root.update()
                if compare.scene is None or len(compare.canvas.find_all()) < 15:
                    raise RuntimeError('Comparison chart did not render: ' + kind)
            from .graph_style_ui import GraphStyleDialog
            from types import SimpleNamespace
            formatter = GraphStyleDialog(compare)
            root.update()
            formatter.labels['x'].delete('1.0', 'end')
            formatter.labels['x'].insert('1.0', 'Time, t / s')
            formatter.labels['x'].tag_add('sel', '1.6', '1.7')
            formatter.mark(formatter.labels['x'], 'italic')
            formatter.labels['y'].delete('1.0', 'end')
            # σ は Symbol、-1 は上付きで描けることを実際に確かめる。
            formatter.labels['y'].insert('1.0', '熱流, σ / mW g-1')
            formatter.labels['y'].tag_add('sel', '1.4', '1.5')
            formatter.mark(formatter.labels['y'], 'symbol')
            formatter.labels['y'].tag_remove('sel', '1.0', 'end')
            formatter.labels['y'].tag_add('sel', '1.12', '1.14')
            formatter.mark(formatter.labels['y'], 'super')
            formatter.labels['y'].tag_remove('sel', '1.0', 'end')
            formatter.variables['english_font'].set('Times New Roman')
            formatter.variables['japanese_font'].set('MS 明朝')
            formatter.variables['legend_position'].set('自由配置')
            name = compare.series_names()[0]
            formatter.series_fields[name][0].set('Package legend')
            formatter.series_fields[name][1].set('#0077CC')
            formatter.series_fields[name][2].set('三角')
            formatter.apply(notify=False)
            compare.kind.set('散布図')
            compare.refresh()
            root.update()
            if compare.graph_style.x_italics != ((6, 7),) or 'Package legend' not in compare.scene.svg():
                raise RuntimeError('Custom legend and partial italics were not applied.')
            if compare.graph_style.y_symbols != ((4, 5),) or compare.graph_style.y_supers != ((12, 14),):
                raise RuntimeError('Symbol and superscript ranges were not stored.')
            drawn = compare.scene.svg()
            if 'font-family="Symbol"' not in drawn or '\uf073' not in drawn:
                raise RuntimeError('Greek was not drawn with the Symbol font code points.')
            # 元の σ は metadata に残る。描画された tspan に出ていないことを見る。
            if '>σ<' in drawn or ' dy="' not in drawn:
                raise RuntimeError('Superscript shift or Symbol substitution was not applied.')
            if 'Times New Roman' not in compare.scene.svg() or 'MS Mincho' not in compare.scene.svg():
                raise RuntimeError('Selected font families were not retained in SVG.')
            # Configure is preview-only: even the scene object and SVG bytes stay fixed.
            original_scene, original_svg = compare.scene, compare.scene.svg()
            for geometry in ('950x740', '1450x1000'):
                compare.geometry(geometry)
                root.update()
                if compare.scene is not original_scene or compare.scene.svg() != original_svg:
                    raise RuntimeError('Window size changed the physical figure or exported SVG.')
            compare.figure_controls.mode.current(1)
            compare.figure_controls.mode.event_generate('<<ComboboxSelected>>')
            root.update()
            if compare.scene.svg() != original_svg:
                raise RuntimeError('Preview zoom changed exported SVG.')
            compare.figure_controls.mode.current(0)
            compare.figure_controls.mode.event_generate('<<ComboboxSelected>>')
            root.update()
            report['figure_preview_resize_invariant'] = True
            compare.figure_controls.preset.set('論文（1段・暫定）')
            compare.figure_controls.choose()
            if compare.scene.width_mm != 84 or compare.scene.style.label_size != 9:
                raise RuntimeError('Figure preset toolbar did not change physical size and fonts.')
            from .figure_presets import FIGURE_PRESETS
            for name in FIGURE_PRESETS:
                compare.figure_controls.preset.set(name)
                compare.figure_controls.choose()
                root.update()
                if compare.scene is None:
                    raise RuntimeError('Built-in figure preset failed: ' + name + ': ' + compare.scene_error)
                ox, oy = compare.preview_offset
                right = ox + compare.scene.width * compare.preview_scale
                bottom = oy + compare.scene.height * compare.preview_scale
                for item in compare.canvas.find_all():
                    if compare.canvas.type(item) == 'text':
                        left, top, end, base = compare.canvas.bbox(item)
                        if left < ox-4 or top < oy-4 or end > right+4 or base > bottom+4:
                            raise RuntimeError('Native text extends outside the physical figure: ' + name)
            report['figure_builtin_text_bounds'] = True
            formatter = GraphStyleDialog(compare)
            formatter.preset.set('日本語資料')
            formatter.select_preset()
            if float(formatter.variables['label_size'].get()) != 10.5:
                raise RuntimeError('Figure preset did not populate fractional point size.')
            formatter.variables['width_mm'].set('140')
            formatter.variables['height_mm'].set('85')
            formatter.as_default.set(True)
            from unittest.mock import patch
            with patch('fileclient.graph_style_ui.simpledialog.askstring', return_value='PACKAGE-WIDE'), \
                    patch('fileclient.graph_style_ui.messagebox.showinfo'):
                formatter.save_preset()
            formatter.apply(notify=False)
            root.update()
            if compare.scene.width_mm != 140 or compare.scene.style.label_size != 10.5:
                raise RuntimeError('Personal figure dimensions or fractional point size were lost.')
            if compare.graph_style.y_symbols != ((4, 5),):
                raise RuntimeError('Changing figure presets erased axis markup.')
            report['figure_personal_preset'] = True
            report['figure_dimensions_mm'] = [compare.scene.width_mm, compare.scene.height_mm]
            bounds = compare.scene.legend_bounds
            def scene_event(x, y):
                ox, oy = compare.preview_offset
                return SimpleNamespace(x=ox+x*compare.preview_scale, y=oy+y*compare.preview_scale)
            compare.press_chart(scene_event(bounds[0]+2, bounds[1]+2))
            compare.drag_legend(scene_event(bounds[0]+22, bounds[1]+22))
            compare.legend_drag = None
            if compare.graph_style.legend_position != '自由配置':
                raise RuntimeError('Legend dragging did not retain a custom position.')
            compare.table.selection_set(measurement['id'])
            compare.show_provenance()
            if measurement['revision_id'] not in compare.detail.cget('text'):
                raise RuntimeError('Chart provenance must identify the measurement revision.')
            export_comparison(compare.selected_rows, Path(temporary) / 'comparison.csv', raw=True)
            export_svg(compare.scene, Path(temporary) / 'comparison.svg')
            compare.destroy()
            # Exercise the new native tools with XLSX input and real callbacks.
            from .bulk_import import export_template
            from .bulk_import_ui import BulkImportDialog
            from .tables import read_table
            import openpyxl
            workbook_path = Path(temporary) / '一括登録.xlsx'
            export_template('measurement', workbook_path)
            workbook = openpyxl.load_workbook(workbook_path)
            worksheet = workbook.active
            worksheet.cell(2, 2, 'PACKAGE-DEMO')
            worksheet.cell(2, 3, 'zz00000')
            worksheet.cell(2, 4, 'zz00000')
            worksheet.append([cell.value for cell in worksheet[2]])
            worksheet.cell(3, 9, '2.4')
            workbook.save(workbook_path)
            workbook.close()
            bulk = BulkImportDialog(app)
            root.update()
            bulk.table = read_table(workbook_path)
            bulk.reset_mapping()
            bulk.preview()
            if len(bulk.entries) != 1 or bulk.entries[0].status != '登録可能' or bulk.entries[0].data['values'] != ['1.2', '2.4']:
                raise RuntimeError('XLSX grouped import preview failed.')
            bulk.save()
            wait_job()
            if app.pending_count != 4:
                raise RuntimeError('Bulk import did not save exactly one local pending record.')
            bulk.preview()
            if bulk.entries[0].status != '取込済み':
                raise RuntimeError('Repeated workbook import was not detected.')
            bulk.destroy()
            from .candidates_ui import CandidateDialog
            candidate_form = RecordDialog(app, 'sample')
            candidate = CandidateDialog(candidate_form, 'alloy')
            candidate.value.insert(0, 'A7777-PACKAGE')
            candidate.save()
            if candidate_form.fields['alloy'].get() != 'A7777-PACKAGE':
                raise RuntimeError('Inline canonical candidate addition failed.')
            candidate_form.grab_release()
            candidate_form.destroy()
            from .polarization_ui import PolarizationDialog
            from .tables import Table
            from .polarization import make_curve
            curves = PolarizationDialog(app, style_file=Path(temporary) / 'curve-style.json',
                                        preset_file=Path(temporary) / 'figure-presets.json')
            curves.table = Table(('電位', '電流'), (('-500', '-.1'), ('-400', '0'), ('-300', '.2'), ('-350', '.3')), 'a'*64)
            fields = dict(e_col='電位', i_col='電流', e_unit='mV', i_unit='mA', area='2', reference='Ag/AgCl', name='PACKAGE-CURVE')
            for key, value in fields.items():
                widget = curves.fields[key]
                if hasattr(widget, 'set'):
                    widget.configure(values=list(curves.table.headers) if key in {'e_col', 'i_col'} else widget.cget('values'))
                    widget.set(value)
                else:
                    widget.insert(0, value)
            root.update()
            curves.add_curve()
            root.update()
            if curves.scene is None or not curves.scene.metadata['acquisition_order_preserved']:
                raise RuntimeError('Native polarization curve did not render.')
            formatter = GraphStyleDialog(curves)
            root.update()
            formatter.series_fields['PACKAGE-CURVE'][0].set('Curve legend')
            formatter.series_fields['PACKAGE-CURVE'][1].set('#0077CC')
            formatter.apply(notify=False)
            root.update()
            if '#0077CC' not in curves.scene.svg() or 'Curve legend' not in curves.scene.svg():
                raise RuntimeError('Polarization legend or color editor did not apply.')
            original_scene, original_svg = curves.scene, curves.scene.svg()
            for geometry in ('1000x730', '1500x1000'):
                curves.geometry(geometry)
                root.update()
                if curves.scene is not original_scene or curves.scene.svg() != original_svg:
                    raise RuntimeError('Polarization export depends on the window size.')
            report['polarization_resize_invariant'] = True
            export_svg(curves.scene, Path(temporary) / 'polarization.svg')
            curves.destroy()
            from .trash_ui import VisibilityDialog, TrashDialog
            # Real native confirmation callbacks; only synthetic local records.
            trash_sample = app.snapshot.current(created['id'])
            guard = VisibilityDialog(app, trash_sample, deleted=True)
            root.update()
            if str(guard.execute.cget('state')) != 'disabled':
                raise RuntimeError('Delete guard must start disabled.')
            guard.reason.insert(0, 'PACKAGE-DELETE-CHECK')
            guard.confirmed.set(True)
            guard.keyword.set('削除')
            guard.update_button()
            guard.apply()
            guard.keyword.set('削除')
            if str(guard.execute.cget('state')) != 'disabled':
                raise RuntimeError('Delete confirmation allowed a repeat while saving.')
            wait_job()
            if any(item['id'] == trash_sample['id'] for item in app.snapshot.of_kind('sample')):
                raise RuntimeError('Deleted sample stayed in the normal list.')
            trash = TrashDialog(app)
            root.update()
            if not trash.tree.get_children():
                raise RuntimeError('The deleted sample was not recoverable from trash.')
            trash.destroy()
            deleted_sample = app.snapshot.current(trash_sample['id'], include_deleted=True)
            guard = VisibilityDialog(app, deleted_sample, deleted=False)
            guard.confirmed.set(True)
            guard.update_button()
            guard.apply()
            wait_job()
            if not any(item['id'] == trash_sample['id'] for item in app.snapshot.of_kind('sample')):
                raise RuntimeError('Restoration did not restore the sample.')
            if errors:
                raise RuntimeError('; '.join(errors))
            app.close()
            root = None
            if session.sync() != (6, [], 0) or session.sync() != (0, [], 0):
                raise RuntimeError('Bundled queue delivery was not idempotent.')
            report.update(ok=True, tkinter=str(tk.TkVersion), forms=5,
                          store_records=len(store.load().heads), offline_queue=True,
                          id_normalization=True, placeholder_is_not_value=True,
                          controlled_choices=True, dependent_alloys=True,
                          storage_controls_in_maintenance=True, sqlite=sqlite3.sqlite_version,
                          sqlite_search=True, form_saves=2, selection_preserved=True,
                          filtered_new_sample_revealed=True, comparison_charts=4,
                          comparison_provenance=True, comparison_raw_csv=True, comparison_svg=True,
                          comparison_export_controls_visible=True, graph_style_editor=True,
                          partial_italics=True, selectable_fonts=True, custom_series_colors=True,
                          custom_legend=True, draggable_legend=True, inward_ticks=True,
                          font_dependencies=app.font_status, symbol_greek_rendered=True,
                          superscript_rendered=True,
                          production_binding=True, unrelated_store_switch_refused=True,
                          production_title=True, attachment_extension_visible=True,
                          png_export_callback=True, missing_extension_supplied=True,
                          xlsx_grouped_import=True, repeated_import_skipped=True,
                          inline_candidate_addition=True, polarization_tool=True,
                          polarization_unit_conversion=True, polarization_style_editor=True,
                          trash_confirmation=True, trash_restore=True, cascade_visibility=True)
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        runtime.profile_directory = original_profile_directory
        if root is not None:
            root.destroy()
        with Path(report_path).open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
    return 0 if report['ok'] else 1


def launch():
    # Used only by the release builder. Normal double-clicks take the GUI path.
    if len(sys.argv) == 3 and sys.argv[1] == '--self-check-report':
        return self_check(sys.argv[2])
    try:
        from .app import main
        main()
        return 0
    except Exception as exc:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror('LabNoteを起動できませんでした',
                'アプリをPCのローカルフォルダへ展開して、もう一度開いてください。'
                f'\n\n詳細: {exc}', parent=root)
            root.destroy()
        except Exception:
            if sys.platform == 'win32':
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, 'LabNoteを起動できませんでした。配布元へご連絡ください。', 'LabNote', 0x10)
        return 1
