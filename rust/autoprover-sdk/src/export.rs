//! The export macro. `#[macro_export]` puts [`export_app!`](crate::export_app) at the crate root
//! regardless of this module.

/// Emit the PyO3 module the Python host loads. Invoke it once in an application crate
/// (a `cdylib` depending on `autoprover-sdk` and `pyo3`):
///
/// ```ignore
/// autoprover_sdk::export_app!(my_app, MyApp::new());
/// ```
///
/// `module_ident` MUST match the wheel's module name. The expansion defines the pure callouts
/// (`descriptor`/`validate_preconditions`/`units`/`author_prompt`/`judge_prompt`/`finalize`) and
/// the two BLOCKING ones (`compile`/`validate`, which release the GIL while `run-confined` runs),
/// all delegating to the [`ffi`](crate::ffi) helpers of the same name.
#[macro_export]
macro_rules! export_app {
    ($module:ident, $ctor:expr) => {
        fn __autoprover_app() -> &'static dyn $crate::Backend {
            static APP: ::std::sync::OnceLock<::std::boxed::Box<dyn $crate::Backend>> =
                ::std::sync::OnceLock::new();
            &**APP.get_or_init(|| ::std::boxed::Box::new($ctor))
        }

        #[$crate::pyo3::pyfunction]
        fn descriptor() -> ::std::string::String {
            $crate::ffi::descriptor(__autoprover_app())
        }

        #[$crate::pyo3::pyfunction]
        fn validate_preconditions(
            args_json: ::std::string::String,
        ) -> ::std::option::Option<::std::string::String> {
            $crate::ffi::validate_preconditions(__autoprover_app(), &args_json)
        }

        #[$crate::pyo3::pyfunction]
        fn units(input_json: ::std::string::String) -> ::std::string::String {
            $crate::ffi::units(__autoprover_app(), &input_json)
        }

        #[$crate::pyo3::pyfunction]
        #[pyo3(signature = (input_json, failure_json=None))]
        fn author_prompt(
            input_json: ::std::string::String,
            failure_json: ::std::option::Option<::std::string::String>,
        ) -> ::std::string::String {
            $crate::ffi::author_prompt(__autoprover_app(), &input_json, failure_json.as_deref())
        }

        #[$crate::pyo3::pyfunction]
        fn judge_prompt(
            input_json: ::std::string::String,
            spec: ::std::string::String,
        ) -> ::std::option::Option<::std::string::String> {
            $crate::ffi::judge_prompt(__autoprover_app(), &input_json, &spec)
        }

        #[$crate::pyo3::pyfunction]
        fn compile(
            py: $crate::pyo3::Python<'_>,
            input_json: ::std::string::String,
            spec: ::std::string::String,
            workdir: ::std::string::String,
            sandbox_json: ::std::string::String,
        ) -> ::std::string::String {
            // Release the GIL for the (minutes-long) build — no async runtime needed.
            py.allow_threads(move || {
                $crate::ffi::compile(__autoprover_app(), &input_json, &spec, &workdir, &sandbox_json)
            })
        }

        #[$crate::pyo3::pyfunction]
        fn validate(
            py: $crate::pyo3::Python<'_>,
            input_json: ::std::string::String,
            spec: ::std::string::String,
            target_json: ::std::string::String,
            workdir: ::std::string::String,
            sandbox_json: ::std::string::String,
        ) -> ::std::string::String {
            py.allow_threads(move || {
                $crate::ffi::validate(
                    __autoprover_app(),
                    &input_json,
                    &spec,
                    &target_json,
                    &workdir,
                    &sandbox_json,
                )
            })
        }

        #[$crate::pyo3::pyfunction]
        fn sandbox_grants(args_json: ::std::string::String) -> ::std::string::String {
            $crate::ffi::sandbox_grants(__autoprover_app(), &args_json)
        }

        #[$crate::pyo3::pyfunction]
        fn workspace_prep(input_json: ::std::string::String) -> ::std::string::String {
            $crate::ffi::workspace_prep(__autoprover_app(), &input_json)
        }

        #[$crate::pyo3::pyfunction]
        fn finalize(
            outcomes_json: ::std::string::String,
        ) -> ::std::option::Option<::std::string::String> {
            $crate::ffi::finalize(__autoprover_app(), &outcomes_json)
        }

        #[$crate::pyo3::pymodule]
        fn $module(
            m: &$crate::pyo3::Bound<'_, $crate::pyo3::types::PyModule>,
        ) -> $crate::pyo3::PyResult<()> {
            use $crate::pyo3::types::PyModuleMethods as _;
            m.add_function($crate::pyo3::wrap_pyfunction!(descriptor, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(validate_preconditions, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(units, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(author_prompt, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(judge_prompt, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(compile, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(validate, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(sandbox_grants, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(workspace_prep, m)?)?;
            m.add_function($crate::pyo3::wrap_pyfunction!(finalize, m)?)?;
            ::std::result::Result::Ok(())
        }
    };
}
